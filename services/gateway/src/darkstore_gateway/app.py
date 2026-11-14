"""The tracking WebSocket endpoint. Protocol: docs/api/realtime.md."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect

from darkstore_common import telemetry
from darkstore_common.settings import Settings
from darkstore_common.tokens import Claims, InvalidToken, verify

from .hub import Conn, Hub
from .queues import Overflow, stream_id_key
from .store import TrackStore

log = logging.getLogger("darkstore.gateway")
CODES = {"unauthorized": 4401, "forbidden": 4403, "bad_request": 4400, "timeout": 4408}


async def _fail(ws: WebSocket, code: str, message: str) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "code": code, "message": message}))
        await ws.close(code=CODES[code])
    except Exception:  # noqa: BLE001 - peer already gone
        pass


def create_app(settings: Settings, store: TrackStore, hub: Hub) -> FastAPI:
    ins = telemetry.instruments()
    app = FastAPI(title="tracking-gateway")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "connections": hub.connections}

    @app.get("/metrics")
    async def metrics():
        body = (f"gateway_ws_connections {hub.connections}\n"
                f"gateway_events_routed_total {hub.events_routed}\n"
                f"gateway_events_dropped_total {hub.events_dropped}\n")
        return Response(body, media_type="text/plain; version=0.0.4")

    async def authenticate(ws: WebSocket) -> Claims | None:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), settings.ws_auth_timeout)
        except asyncio.TimeoutError:
            await _fail(ws, "timeout", "auth frame not received in time")
            return None
        try:
            frame = json.loads(raw) if len(raw) <= settings.ws_max_frame_bytes else None
            if not isinstance(frame, dict) or frame.get("type") != "auth":
                raise InvalidToken("first frame must be auth")
            claims = verify(settings.token_secret, frame.get("token"))
            if claims.role not in ("customer", "ops"):   # e.g. a guest (order-creation) token
                raise InvalidToken("role cannot track orders")
            return claims
        except (InvalidToken, ValueError):
            await _fail(ws, "unauthorized", "invalid or expired token")
            return None

    async def subscribe(conn: Conn, order_id: str, last_event_id: str | None) -> bool:
        c = conn.claims
        if c.role == "customer" and c.order_id != order_id:
            await _fail(conn.ws, "forbidden", "token is not valid for this order")
            return False
        if len(conn.subs) >= settings.ws_max_subscriptions and order_id not in conn.subs:
            await _fail(conn.ws, "bad_request", "too many subscriptions")
            return False
        if last_event_id is not None:
            try:
                stream_id_key(last_event_id)
            except ValueError:
                await _fail(conn.ws, "bad_request", "malformed last_event_id")
                return False
        conn.syncing[order_id] = []          # buffer live events until the snapshot is queued
        hub.add(conn, order_id)
        snap = await store.snapshot(order_id)
        if snap["status"] is None:
            snap["status"] = "created"       # the order exists (it was just placed) but is not ingested yet
        # `last_event_id` from the client is advisory: the snapshot is built atomically with the
        # stream, so it already contains everything up to snapshot.last_event_id. Anything newer
        # arrives via the buffered live events flushed below. That makes resume gap-free by state.
        conn.queue.put(snap, droppable=False)
        hub.flush_synced(conn, order_id, snap["last_event_id"])
        return True

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        allowed = settings.ws_allowed_origins
        if allowed and ws.headers.get("origin") not in allowed:
            await ws.close(code=4403)         # refuse the handshake: not one of our pages
            return
        await ws.accept()
        claims = await authenticate(ws)
        if claims is None:
            return
        conn = Conn(ws, claims)
        cap = settings.ws_max_conns_ops if claims.role == "ops" else settings.ws_max_conns_per_order
        if hub.subject_count(conn.subject) >= cap:
            await _fail(ws, "bad_request", "too many connections for this token")
            return
        hub.admit(conn)
        # server_time lets the client correct its ETA countdown for clock skew
        await ws.send_text(json.dumps({"type": "auth_ok", "role": claims.role, "server_time": time.time()}))

        async def sender():
            while True:
                if not await conn.queue.wait(settings.ws_heartbeat):
                    await ws.send_text(json.dumps({"type": "heartbeat", "ts": time.time()}))
                    continue
                if conn.overflow:
                    raise Overflow("client is not keeping up")
                while (msg := conn.queue.pop()) is not None:
                    # Flow control: the transport buffers without limit, so it cannot tell us a client
                    # stopped reading. Instead we stop sending after `ack_window` unacknowledged
                    # messages; meanwhile the queue drops old positions/ETAs but never a status.
                    while conn.unacked >= settings.ws_ack_window:
                        conn.acked.clear()
                        try:
                            await asyncio.wait_for(conn.acked.wait(), settings.ws_stall_timeout)
                        except asyncio.TimeoutError:
                            raise Overflow("client stopped acknowledging") from None
                    queued_at = msg.pop("_q", None)
                    await asyncio.wait_for(ws.send_text(json.dumps(msg, separators=(",", ":"))), 30)
                    conn.unacked += 1
                    if queued_at is not None:
                        ins.ws_fanout.record(time.monotonic() - queued_at)

        send_task = asyncio.create_task(sender())
        conn_attrs = {"role": claims.role}
        ins.ws_connections.add(1, conn_attrs)
        try:
            while True:
                recv = asyncio.create_task(asyncio.wait_for(ws.receive_text(), settings.ws_idle_timeout))
                done, _ = await asyncio.wait({recv, send_task}, return_when=asyncio.FIRST_COMPLETED)
                if send_task in done:
                    recv.cancel()
                    send_task.result()      # surfaces Overflow / send errors
                    break
                try:
                    raw = recv.result()
                except asyncio.TimeoutError:
                    await _fail(ws, "timeout", "no frames from client")
                    break
                if len(raw) > settings.ws_max_frame_bytes:
                    await _fail(ws, "bad_request", "frame too large")
                    break
                try:
                    frame = json.loads(raw)
                    kind = frame["type"]
                except (ValueError, KeyError, TypeError):
                    await _fail(ws, "bad_request", "malformed frame")
                    break
                if kind == "subscribe":
                    oid = frame.get("order_id")
                    if not isinstance(oid, str) or not oid or len(oid) > 64:
                        await _fail(ws, "bad_request", "order_id required")
                        break
                    if not await subscribe(conn, oid, frame.get("last_event_id")):
                        break
                elif kind in ("ack", "pong"):          # the client is alive and reading
                    conn.unacked = 0
                    conn.acked.set()
                else:
                    await _fail(ws, "bad_request", f"unknown frame type {kind!r}")
                    break
        except (WebSocketDisconnect, Overflow) as e:
            if isinstance(e, Overflow):
                ins.ws_overflow.add(1)
        except Exception:  # noqa: BLE001
            log.exception("websocket handler failed")
        finally:
            ins.ws_connections.add(-1, conn_attrs)
            send_task.cancel()
            hub.remove(conn)
            hub.release(conn)

    return app
