from __future__ import annotations

import asyncio

import uvicorn

from darkstore_common.loops import run_loop
from darkstore_common.service import run_service
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from . import logic
from .api import create_app
from .models import META


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    if not settings.token_secret:
        raise RuntimeError("TOKEN_SECRET is not set: the order service cannot issue or verify tokens")
    async def handler(session, env):
        await logic.apply_event(session, settings, env)

    async def http(sm, stop):
        server = uvicorn.Server(uvicorn.Config(create_app(sm, settings), host="0.0.0.0",
                                               port=settings.http_port, log_level="warning"))
        server.install_signal_handlers = lambda: None
        task = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await task

    async def sweeper(sm, stop):
        async def step():
            async with sm.begin() as s:
                await logic.expire_stuck(s, settings)
        await run_loop(stop, step, max(settings.sweep_interval, 0.5), "order-timeout-sweeper")

    await run_service(settings, meta=META, group="order",
                      consumes=[Topic.INVENTORY_EVENTS, Topic.PICKING_EVENTS, Topic.DISPATCH_EVENTS],
                      produces=[Topic.ORDER_EVENTS], handler=handler, stop=stop,
                      extra=[http, sweeper], ready=ready)
