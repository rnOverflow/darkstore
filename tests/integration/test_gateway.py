"""Tracking gateway over real WebSockets: auth, snapshot-on-connect, live events, resume,
multi-replica fan-out, heartbeats and backpressure. Real Redpanda + Postgres + Redis."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from darkstore_common.riders import make_redis
from darkstore_common.tokens import sign
from darkstore_gateway.queues import stream_id_key
from darkstore_gateway.service import serve as serve_gateway
from darkstore_eta.service import serve as serve_eta
from darkstore_gateway.store import TrackStore

from .helpers import running, until
from .test_dispatch import KM, S0, fleet
from .test_saga import _eq, _port

pytestmark = pytest.mark.integration
SECRET = "test-secret-" + "x" * 24


@asynccontextmanager
async def gateways(env, n=1, **overrides):
    """n gateway replicas on their own ports (same consumer group + same Redis)."""
    ports = [_port() for _ in range(n)]
    st = env.model_copy(update={"token_secret": SECRET, **overrides})
    async with AsyncExitStack() as x:
        for p in ports:
            await x.enter_async_context(running(
                lambda stop, p=p: serve_gateway(st.model_copy(update={"http_port": p}), stop)))
        origins = overrides.get("ws_allowed_origins") or (None,)
        await until(lambda: _listening(ports, origins[0]), timeout=40)
        yield ports


async def _listening(ports, origin=None):
    for p in ports:
        try:
            async with connect(f"ws://localhost:{p}/ws", open_timeout=2, origin=origin):
                pass
        except Exception:  # noqa: BLE001
            return False
    return True


async def open_ws(port, token, order_id=None, last_event_id=None):
    ws = await connect(f"ws://localhost:{port}/ws")
    await ws.send(json.dumps({"type": "auth", "token": token}))
    if order_id:
        await ws.send(json.dumps({"type": "subscribe", "order_id": order_id,
                                  "last_event_id": last_event_id}))
    return ws


async def recv(ws, timeout=30):
    """Receive one JSON frame, acking every 50th like the real client does (flow control)."""
    m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    ws.n_recv = getattr(ws, "n_recv", 0) + 1
    if ws.n_recv % 50 == 0:
        await ws.send(json.dumps({"type": "ack"}))
    return m


async def expect_close(ws, code, timeout=10):
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout)
    except ConnectionClosed as e:
        assert e.rcvd.code == code, f"closed with {e.rcvd.code}, wanted {code}"


async def test_unauthenticated_and_badly_authenticated_connections_are_refused(env):
    async with gateways(env, ws_auth_timeout=1.0) as (port,):
        ws = await connect(f"ws://localhost:{port}/ws")        # sends subscribe instead of auth
        await ws.send(json.dumps({"type": "subscribe", "order_id": "x"}))
        assert (await recv(ws))["code"] == "unauthorized"
        await expect_close(ws, 4401)
        for bad in ("garbage", sign("another-secret-" + "y" * 20, role="ops", ttl_s=60),
                    sign(SECRET, role="ops", ttl_s=-5),
                    sign(SECRET, role="guest", customer_id="g-1", ttl_s=60)):   # order-creation token: no tracking
            ws = await open_ws(port, bad)
            assert (await recv(ws))["code"] == "unauthorized"
            await expect_close(ws, 4401)
        ws = await connect(f"ws://localhost:{port}/ws")        # says nothing at all
        assert (await recv(ws))["code"] == "timeout"
        await expect_close(ws, 4408)


async def test_a_customer_token_cannot_watch_someone_elses_order(env):
    async with gateways(env) as (port,):
        ws = await open_ws(port, sign(SECRET, role="customer", order_id="mine", ttl_s=60), "theirs")
        assert (await recv(ws))["type"] == "auth_ok"
        err = await recv(ws)
        assert err["code"] == "forbidden"
        await expect_close(ws, 4403)
        ws = await open_ws(port, sign(SECRET, role="ops", ttl_s=60), "anything")   # ops may watch any
        assert (await recv(ws))["role"] == "ops" and (await recv(ws))["type"] == "snapshot"


async def test_malformed_frames_and_oversized_frames_close_the_socket_cleanly(env):
    async with gateways(env, ws_max_frame_bytes=200) as (port,):
        tok = sign(SECRET, role="ops", ttl_s=60)
        for payload in ("not json", json.dumps({"type": "teleport"}), json.dumps({"type": "subscribe"}),
                        json.dumps({"type": "subscribe", "order_id": "o", "last_event_id": "zzz"})):
            ws = await open_ws(port, tok)
            await recv(ws)
            await ws.send(payload)
            assert (await recv(ws))["code"] == "bad_request"
            await expect_close(ws, 4400)
        # an oversized frame never reaches the app: the server's own size cap closes it (1009)
        ws = await open_ws(port, tok)
        await recv(ws)
        await ws.send(json.dumps({"type": "subscribe", "order_id": "x" * 500}))
        await expect_close(ws, 1009)


async def test_heartbeats_flow_and_a_silent_client_is_dropped(env):
    async with gateways(env, ws_heartbeat=0.5, ws_idle_timeout=2.0) as (port,):
        ws = await open_ws(port, sign(SECRET, role="ops", ttl_s=60), "o")
        beats = 0
        while beats < 2:
            m = await recv(ws, 5)
            beats += m["type"] == "heartbeat"
        await expect_close(ws, 4408, timeout=10)            # never answered with pong


async def collect_until_delivered(ws, timeout=120):
    frames, deadline = [], time.time() + timeout
    while time.time() < deadline:
        m = await recv(ws, timeout)
        frames.append(m)
        if m["type"] == "event" and m["kind"] == "status" and m["data"]["status"] in ("delivered", "cancelled"):
            return frames
    raise AssertionError("order was not delivered in time")


async def new_order(c, token_needed=True):
    r = await c.http.post("/orders", json={
        "customer_id": "c1", "store_id": "store-0", "lat": S0[0] + 1.0 * KM, "lng": S0[1],
        "items": [{"sku": "sku-1", "qty": 1}]})
    assert r.status_code == 201, r.text
    return r.json()


async def test_customer_watches_an_order_from_creation_to_delivery(env):
    async with fleet(env, seed=[("r1", S0[0] + 0.4 * KM, S0[1], False)], token_secret=SECRET,
                     extra=(serve_eta,), eta_model_enabled=False) as c:
        async with gateways(env) as (port,):
            o = await new_order(c)
            assert o["tracking_token"]
            ws = await open_ws(port, o["tracking_token"], o["order_id"])
            assert (await recv(ws))["type"] == "auth_ok"
            snap = await recv(ws)
            assert snap["type"] == "snapshot" and snap["order_id"] == o["order_id"]
            frames = [snap, *await collect_until_delivered(ws)]
            events = [f for f in frames if f["type"] == "event"]
            statuses = [e["data"]["status"] for e in events if e["kind"] == "status"]
            order = ["created", "reserved", "picking", "picked", "assigned", "out_for_delivery", "delivered"]
            seen = ([snap["status"]] if snap["status"] else []) + statuses
            assert seen[-1] == "delivered"
            assert [order.index(s) for s in seen] == sorted(order.index(s) for s in seen)   # forward only
            kinds = {e["kind"] for e in events}
            assert {"status", "eta", "position"} <= kinds
            ids = [e["id"] for e in events]
            assert len(set(ids)) == len(ids)
            etas = [e["data"] for e in events if e["kind"] == "eta"]
            assert all(0 < d["p50_real_s"] <= d["p90_real_s"] for d in etas)
            pos = [e["data"] for e in events if e["kind"] == "position"]
            assert pos and all(abs(p["lat"] - S0[0]) < 0.2 for p in pos)


async def test_reconnect_with_last_event_id_resumes_without_losing_any_status(env):
    async with fleet(env, seed=[("r1", S0[0] + 0.4 * KM, S0[1], False)], token_secret=SECRET) as c:
        async with gateways(env) as (port,):
            o = await new_order(c)
            ws = await open_ws(port, o["tracking_token"], o["order_id"])
            last, applied = None, []
            while True:                                           # watch until a rider is assigned
                m = await recv(ws, 60)
                if m["type"] == "event":
                    last = m["id"]
                    if m["kind"] == "status":
                        applied.append(m["data"]["status"])
                        if m["data"]["status"] in ("assigned", "out_for_delivery"):
                            break
            await ws.close()                                      # network drops here
            await asyncio.sleep(2)                                # ... and events keep happening
            ws2 = await open_ws(port, o["tracking_token"], o["order_id"], last_event_id=last)
            await recv(ws2)                                       # auth_ok
            snap = await recv(ws2)
            assert snap["type"] == "snapshot" and snap["last_event_id"]
            assert stream_id_key(snap["last_event_id"]) >= stream_id_key(last)
            held = [h["status"] for h in snap["history"]]
            assert set(applied) <= set(held)                      # nothing already seen was lost
            rest = await collect_until_delivered(ws2) if snap["status"] != "delivered" else []
            for m in rest:
                if m["type"] == "event":
                    assert stream_id_key(m["id"]) > stream_id_key(snap["last_event_id"])
            final = snap["status"] if not rest else [m for m in rest if m["type"] == "event" and m["kind"] == "status"][-1]["data"]["status"]
            assert final == "delivered"


async def test_every_gateway_replica_delivers_to_its_own_clients(env):
    async with fleet(env, seed=[("r1", S0[0] + 0.4 * KM, S0[1], False)], token_secret=SECRET) as c:
        async with gateways(env, n=2) as (p1, p2):
            o = await new_order(c)
            ws1 = await open_ws(p1, o["tracking_token"], o["order_id"])
            ws2 = await open_ws(p2, o["tracking_token"], o["order_id"])
            r1, r2 = await asyncio.gather(collect_until_delivered(ws1), collect_until_delivered(ws2))
            for frames in (r1, r2):
                st = [f["data"]["status"] for f in frames if f["type"] == "event" and f["kind"] == "status"]
                assert st[-1] == "delivered"


async def test_a_client_that_stops_reading_loses_positions_but_never_a_status(env):
    async with gateways(env, ws_heartbeat=30, ws_idle_timeout=120, ws_ack_window=100) as (port,):
        rds = make_redis(env.redis_url, max_connections=8)
        store = TrackStore(rds)                                   # same "ds:" prefix the gateway uses
        oid = f"flood-{uuid.uuid4().hex[:6]}"
        ws = await open_ws(port, sign(SECRET, role="ops", ttl_s=300), oid)
        await recv(ws)
        await recv(ws)                                            # snapshot; now we STOP reading
        pad = "x" * 100
        async def pos(i):
            await store.ingest(oid, uuid.uuid4().hex, "position",
                               {"rider_id": pad, "lat": 12.0 + i * 1e-6, "lng": 77.0, "at": "t"})
        await store.ingest(oid, uuid.uuid4().hex, "status", {"status": "created", "at": "t"}, status="created")
        for i in range(8000):
            await pos(i)
        await store.ingest(oid, uuid.uuid4().hex, "status", {"status": "picking", "at": "t"}, status="picking")
        for i in range(8000, 16000):
            await pos(i)
        await store.ingest(oid, uuid.uuid4().hex, "status", {"status": "assigned", "at": "t"}, status="assigned")
        for i in range(16000, 20000):
            await pos(i)
        await asyncio.sleep(1)
        await ws.send(json.dumps({"type": "ack"}))                # the client wakes up and starts acking
        got_status, got_pos, deadline = [], 0, time.time() + 40
        while time.time() < deadline:                             # now drain everything that was kept
            try:
                m = await recv(ws, 3)
            except asyncio.TimeoutError:
                break
            if m["type"] == "event":
                if m["kind"] == "status":
                    got_status.append(m["data"]["status"])
                else:
                    got_pos += 1
        assert got_status == ["created", "picking", "assigned"], got_status      # every status, in order
        assert got_pos < 20000 // 10, f"expected most positions dropped for a stalled client, got {got_pos}"
        await rds.aclose()


async def test_a_client_that_never_acks_is_cut_off_after_the_stall_timeout(env):
    async with gateways(env, ws_ack_window=5, ws_stall_timeout=2.0, ws_heartbeat=30, ws_idle_timeout=120) as (port,):
        rds = make_redis(env.redis_url, max_connections=8)
        store = TrackStore(rds)
        oid = f"stall-{uuid.uuid4().hex[:6]}"
        ws = await open_ws(port, sign(SECRET, role="ops", ttl_s=300), oid)
        await recv(ws)
        for i in range(60):                                        # far more than the 5-message window
            await store.ingest(oid, uuid.uuid4().hex, "position", {"rider_id": "r", "lat": i, "lng": 0, "at": "t"})
        got = 0
        try:
            while True:
                await asyncio.wait_for(ws.recv(), 10)             # never acks
                got += 1
        except ConnectionClosed:
            pass
        assert 5 <= got <= 12                                      # window + snapshot, then silence, then closed
        await rds.aclose()


async def test_subscribing_before_the_gateway_has_seen_the_order_still_gets_a_snapshot_then_events(env):
    async with gateways(env) as (port,):
        rds = make_redis(env.redis_url, max_connections=8)
        store = TrackStore(rds)
        oid = f"early-{uuid.uuid4().hex[:6]}"
        ws = await open_ws(port, sign(SECRET, role="customer", order_id=oid, ttl_s=300), oid)
        await recv(ws)
        snap = await recv(ws)
        assert snap["type"] == "snapshot" and snap["status"] == "created" and snap["last_event_id"] is None
        await store.ingest(oid, uuid.uuid4().hex, "status", {"status": "reserved", "at": "t"}, status="reserved")
        ev = await recv(ws)
        assert ev["type"] == "event" and ev["data"]["status"] == "reserved"
        await rds.aclose()


async def test_clients_resync_after_a_pubsub_outage_instead_of_missing_events_forever(env):
    async with gateways(env) as (port,):
        rds = make_redis(env.redis_url, max_connections=8)
        store = TrackStore(rds)
        oid = f"blip-{uuid.uuid4().hex[:6]}"
        ws = await open_ws(port, sign(SECRET, role="ops", ttl_s=300), oid)
        await recv(ws)
        await recv(ws)                                              # snapshot
        await store.ingest(oid, uuid.uuid4().hex, "status", {"status": "created", "at": "t"}, status="created")
        assert (await recv(ws))["data"]["status"] == "created"
        await rds.execute_command("CLIENT", "KILL", "TYPE", "pubsub")   # the listener loses its connection
        await store.ingest(oid, uuid.uuid4().hex, "status", {"status": "assigned", "at": "t"}, status="assigned")
        seen = None
        for _ in range(30):                                         # an event OR a resync snapshot reveals it
            m = await recv(ws, 10)
            if m["type"] == "snapshot" and m["status"] == "assigned":
                seen = m
                break
            if m["type"] == "event" and m["data"].get("status") == "assigned":
                seen = m
                break
        assert seen is not None, "the outage swallowed the status and nothing resynced the client"
        await rds.aclose()


async def test_connections_from_a_foreign_origin_are_refused_when_an_allowlist_is_set(env):
    async with gateways(env, ws_allowed_origins=("http://good.example",)) as (port,):
        ok = await connect(f"ws://localhost:{port}/ws", origin="http://good.example")
        await ok.send(json.dumps({"type": "auth", "token": sign(SECRET, role="ops", ttl_s=60)}))
        assert (await recv(ok))["type"] == "auth_ok"
        with pytest.raises(Exception) as e:
            await connect(f"ws://localhost:{port}/ws", origin="http://evil.example")
        assert "403" in str(e.value) or "rejected" in str(e.value).lower()


async def test_one_token_cannot_open_unlimited_connections(env):
    async with gateways(env, ws_max_conns_per_order=2) as (port,):
        tok = sign(SECRET, role="customer", order_id="o-cap", ttl_s=300)
        a, b = await open_ws(port, tok), await open_ws(port, tok)
        assert (await recv(a))["type"] == "auth_ok" and (await recv(b))["type"] == "auth_ok"
        c = await open_ws(port, tok)
        assert (await recv(c))["code"] == "bad_request"
        await expect_close(c, 4400)
        await a.close()
        await asyncio.sleep(0.5)
        d = await open_ws(port, tok)                                 # a slot freed up
        assert (await recv(d))["type"] == "auth_ok"
