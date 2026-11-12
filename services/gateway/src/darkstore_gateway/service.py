from __future__ import annotations

import asyncio

import uvicorn

from darkstore_common.riders import make_redis
from darkstore_common.service import run_service
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from .app import create_app
from .hub import Hub, run_pubsub
from .store import TrackStore
from .tracking import derive

from darkstore_common.db import service_meta

META = service_meta("gateway")   # only used for the (idle) outbox table the shared runtime creates


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    if not settings.token_secret:
        raise RuntimeError("TOKEN_SECRET is not set: the gateway cannot verify tokens")
    redis = make_redis(settings.redis_url, max_connections=256)
    store, hub = TrackStore(redis), Hub()

    async def handler(_session, env):
        d = derive(env.event_type, env.payload, env.occurred_at.isoformat())
        if d is None:
            return
        kind, data, meta = d
        await store.ingest(env.order_id, env.event_id, kind, data,
                           status=data["status"] if kind == "status" else None, meta=meta,
                           ts=env.occurred_at.timestamp())

    async def http(sm, stop):
        server = uvicorn.Server(uvicorn.Config(create_app(settings, store, hub), host="0.0.0.0",
                                               port=settings.http_port, log_level="warning",
                                               ws_ping_interval=None,
                                               ws_max_size=settings.ws_max_frame_bytes))
        server.install_signal_handlers = lambda: None
        task = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await task

    async def listener(sm, stop):
        await run_pubsub(redis, store, hub, stop)

    try:
        await run_service(
            settings, meta=META, group="gateway",
            consumes=[Topic.ORDER_EVENTS, Topic.INVENTORY_EVENTS, Topic.PICKING_EVENTS,
                      Topic.DISPATCH_EVENTS, Topic.ETA_EVENTS, Topic.RIDER_POSITIONS],
            handler=handler, stop=stop, extra=[http, listener], ready=ready, dedupe=False)
    finally:
        await redis.aclose()
