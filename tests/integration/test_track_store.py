"""TrackStore against real Redis: atomic ingest, forward-only status, snapshot, replay, pub/sub."""
import asyncio
import json
import uuid

import pytest

from darkstore_gateway.queues import stream_id_key
from darkstore_gateway.store import TrackStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def store(redis_url):
    from darkstore_common.riders import make_redis
    r = make_redis(redis_url)
    yield TrackStore(r, prefix=f"t{uuid.uuid4().hex[:8]}:")
    await r.aclose()


def eid():
    return uuid.uuid4().hex


async def status(store, order, s, at="2026-01-01T00:00:00+00:00", **kw):
    return await store.ingest(order, eid(), "status", {"status": s, "at": at}, status=s, **kw)


async def test_ingest_returns_ascending_stream_ids_and_builds_the_snapshot(store):
    meta = {"store": {"id": "store-0", "lat": 12.9, "lng": 77.6}, "dest": {"lat": 12.95, "lng": 77.62}}
    a = await status(store, "o1", "created", meta=meta)
    b = await status(store, "o1", "reserved")
    assert stream_id_key(a) < stream_id_key(b)
    await store.ingest("o1", eid(), "eta", {"stage": "created", "p50_real_s": 30.0, "p90_real_s": 50.0,
                                            "source": "model", "at": "t"})
    await store.ingest("o1", eid(), "position", {"rider_id": "r1", "lat": 1.0, "lng": 2.0, "at": "t"})
    snap = await store.snapshot("o1")
    assert snap["type"] == "snapshot" and snap["status"] == "reserved"
    assert snap["store"]["id"] == "store-0" and snap["dest"] == {"lat": 12.95, "lng": 77.62}
    assert snap["eta"]["p50_real_s"] == 30.0 and snap["rider"] == {"rider_id": "r1", "lat": 1.0, "lng": 2.0, "at": "t"}
    assert [h["status"] for h in snap["history"]] == ["created", "reserved"]
    assert snap["last_event_id"] is not None and stream_id_key(snap["last_event_id"]) > stream_id_key(b)


async def test_unknown_order_snapshot_is_empty_not_an_error(store):
    snap = await store.snapshot("never-seen")
    assert snap["status"] is None and snap["last_event_id"] is None and snap["history"] == []


async def test_duplicate_event_id_is_ingested_once(store):
    e = eid()
    first = await store.ingest("o", e, "status", {"status": "created", "at": "t"}, status="created")
    again = await store.ingest("o", e, "status", {"status": "created", "at": "t"}, status="created")
    assert first and again is None


async def test_status_only_moves_forward_and_nothing_follows_a_terminal_state(store):
    assert await status(store, "o", "picking")
    assert await status(store, "o", "reserved") is None          # stale, behind us
    assert await status(store, "o", "picking") is None           # same state again
    assert await status(store, "o", "assigned")
    assert await status(store, "o", "delivered")
    assert await status(store, "o", "cancelled") is None         # terminal is final
    assert await store.ingest("o", eid(), "position", {"rider_id": "r", "lat": 0, "lng": 0, "at": "t"}) is None
    assert await store.ingest("o", eid(), "eta", {"stage": "x", "p50_real_s": 1, "p90_real_s": 2,
                                                   "source": "model", "at": "t"}) is None
    assert (await store.snapshot("o"))["status"] == "delivered"


async def test_replay_returns_exactly_the_events_after_an_id_in_order(store):
    ids = [await status(store, "o", s) for s in ("created", "reserved", "picking", "picked")]
    got = await store.replay("o", after_id=ids[1])
    assert [e["id"] for e in got] == ids[2:] and [e["data"]["status"] for e in got] == ["picking", "picked"]
    assert all(e["kind"] == "status" for e in got)
    assert await store.replay("o", after_id=ids[-1]) == []
    assert await store.replay("o", after_id=None) == []


async def test_every_ingested_event_is_published_for_other_gateway_replicas(store):
    pub = store.r.pubsub()
    await pub.psubscribe(store.pattern)
    await pub.get_message(timeout=2)                              # subscribe confirmation
    i = await status(store, "o9", "created")
    msg = None
    for _ in range(20):
        m = await pub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if m:
            msg = m
            break
    assert msg and msg["channel"] == store.channel("o9")
    body = json.loads(msg["data"])
    assert body == {"order_id": "o9", "id": i, "kind": "status", "data": {"status": "created", "at": "2026-01-01T00:00:00+00:00"}}
    await pub.aclose()


async def test_the_per_order_stream_is_bounded(store):
    small = TrackStore(store.r, prefix=store.p, maxlen=20)
    for i in range(300):
        await small.ingest("o", eid(), "position", {"rider_id": "r", "lat": i, "lng": 0, "at": "t"})
    assert await store.r.xlen(f"{store.p}track:stream:o") < 100


async def test_a_late_or_redelivered_position_or_eta_cannot_move_state_backwards(store):
    pos = lambda lat: {"rider_id": "r", "lat": lat, "lng": 0, "at": "t"}  # noqa: E731
    assert await store.ingest("o", eid(), "position", pos(1.0), ts=100.0)
    assert await store.ingest("o", eid(), "position", pos(2.0), ts=200.0)
    assert await store.ingest("o", eid(), "position", pos(1.5), ts=150.0) is None   # older than newest
    assert (await store.snapshot("o"))["rider"]["lat"] == 2.0
    eta = lambda s: {"stage": "x", "p50_real_s": s, "p90_real_s": s + 1, "source": "model", "at": "t"}  # noqa: E731
    assert await store.ingest("o", eid(), "eta", eta(30.0), ts=300.0)
    assert await store.ingest("o", eid(), "eta", eta(99.0), ts=299.0) is None
    assert (await store.snapshot("o"))["eta"]["p50_real_s"] == 30.0
    assert await store.ingest("o", eid(), "position", pos(3.0), ts=200.0)           # equal time is fine
