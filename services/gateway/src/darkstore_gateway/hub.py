"""Local connection registry + the pub/sub listener that feeds it.

Every gateway replica runs one listener on `track:ch:*`. The replica that consumed an event from
Kafka writes it to Redis (stream + PUBLISH); EVERY replica receives the publish and forwards it to
its own sockets. So fan-out works no matter which replica a client is connected to.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket
from redis.asyncio import Redis

from darkstore_common.tokens import Claims

from .queues import Overflow, OutQueue, stream_id_key
from .store import TrackStore

log = logging.getLogger("darkstore.gateway")


class Conn:
    def __init__(self, ws: WebSocket, claims: Claims, max_droppable: int = 50):
        self.ws, self.claims = ws, claims
        self.subject = claims.order_id or "ops"     # what the per-subject connection cap counts
        self.queue = OutQueue(max_droppable=max_droppable)
        self.subs: set[str] = set()
        self.syncing: dict[str, list[dict]] = {}    # live events buffered while a snapshot loads
        self.overflow = False                       # set by the hub; the sender then closes the socket
        self.floor: dict[str, str] = {}             # per order: ids <= this are already in the snapshot
        self.unacked = 0                            # sent since the client last acked (flow control)
        self.acked = asyncio.Event()

    def push(self, msg: dict) -> None:
        out = {"type": "event", "id": msg["id"], "kind": msg["kind"], "data": msg["data"]}
        self.queue.put(out, droppable=msg["kind"] != "status")


class Hub:
    def __init__(self):
        self.by_order: dict[str, set[Conn]] = defaultdict(set)
        self._conns: set[Conn] = set()
        self.events_routed = 0
        self.events_dropped = 0

    @property
    def connections(self) -> int:
        return len({c for s in self.by_order.values() for c in s})

    def add(self, conn: Conn, order_id: str) -> None:
        self.by_order[order_id].add(conn)
        conn.subs.add(order_id)

    def remove(self, conn: Conn) -> None:
        for oid in list(conn.subs):
            self.by_order[oid].discard(conn)
            if not self.by_order[oid]:
                del self.by_order[oid]
        self.events_dropped += conn.queue.dropped
        conn.subs.clear()

    def subject_count(self, subject: str) -> int:
        return sum(1 for c in self._conns if c.subject == subject)

    def admit(self, conn: "Conn") -> None:
        self._conns.add(conn)

    def release(self, conn: "Conn") -> None:
        self._conns.discard(conn)

    async def resync(self, store: TrackStore) -> None:
        """After a pub/sub outage events may have been missed: send every subscriber a fresh snapshot
        (the client's reducer applies it through the same forward-only guard as live events)."""
        for order_id, conns in list(self.by_order.items()):
            snap = await store.snapshot(order_id)
            for conn in list(conns):
                if order_id in conn.syncing:
                    continue                  # a snapshot is already on its way
                try:
                    conn.queue.put(dict(snap), droppable=False)
                    if snap["last_event_id"]:
                        conn.floor[order_id] = snap["last_event_id"]
                except Overflow:
                    conn.overflow = True
                    conn.queue.kick()

    def route(self, msg: dict) -> None:
        for conn in list(self.by_order.get(msg["order_id"], ())):
            self.events_routed += 1
            buf = conn.syncing.get(msg["order_id"])
            try:
                if buf is not None:
                    buf.append(msg)           # snapshot in flight: flushed after it is sent
                else:
                    floor = conn.floor.get(msg["order_id"])
                    if floor is None or stream_id_key(msg["id"]) > stream_id_key(floor):
                        conn.push(msg)
            except Overflow:
                log.warning("connection overflow for order %s: closing it", msg["order_id"])
                conn.overflow = True
                conn.queue.kick()             # wake the sender so it raises and closes the socket

    def flush_synced(self, conn: Conn, order_id: str, snapshot_id: str | None) -> None:
        if snapshot_id is not None:
            conn.floor[order_id] = snapshot_id
        for msg in conn.syncing.pop(order_id, []):
            if snapshot_id is None or stream_id_key(msg["id"]) > stream_id_key(snapshot_id):
                conn.push(msg)               # newer than the snapshot; older ones are already in it


async def run_pubsub(redis: Redis, store: TrackStore, hub: Hub, stop: asyncio.Event) -> None:
    blipped = False
    while not stop.is_set():
        pub = redis.pubsub()
        try:
            await pub.psubscribe(store.pattern)
            if blipped:                       # we may have missed publishes while disconnected
                blipped = False
                await hub.resync(store)
            while not stop.is_set():
                m = await pub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if m:
                    hub.route(json.loads(m["data"]))
        except Exception:  # noqa: BLE001 - Redis blip: resubscribe (clients resync via snapshot)
            log.exception("pub/sub listener failed; resubscribing")
            blipped = True
            await asyncio.sleep(1)
        finally:
            try:
                await pub.aclose()
            except Exception:  # noqa: BLE001
                pass
