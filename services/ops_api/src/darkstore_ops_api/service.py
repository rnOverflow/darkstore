from __future__ import annotations

import asyncio

import uvicorn

from darkstore_common.db import make_engine
from darkstore_common.riders import make_redis
from darkstore_common.settings import Settings

from .app import create_app


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    if not settings.token_secret:
        raise RuntimeError("TOKEN_SECRET is not set: the ops API cannot verify tokens")
    engine, redis = make_engine(settings.database_url), make_redis(settings.redis_url)
    server = uvicorn.Server(uvicorn.Config(create_app(settings, engine, redis), host="0.0.0.0",
                                           port=settings.http_port, log_level="warning"))
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve())
    if ready:
        ready.set()
    await stop.wait()
    server.should_exit = True
    await task
    await engine.dispose()
    await redis.aclose()
