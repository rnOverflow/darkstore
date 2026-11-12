from __future__ import annotations

import asyncio

from darkstore_common.service import run_service
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from . import logic
from .models import META


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    async def handler(session, env):
        if env.event_type == "order.created":
            await logic.reserve(session, settings, env)
        elif env.event_type == "order.cancelled":
            await logic.release(session, env)

    async def seed(sm, stop):
        if settings.seed_stock:
            async with sm.begin() as s:
                await logic.seed_default(s)
        await stop.wait()

    await run_service(settings, meta=META, group="inventory", consumes=[Topic.ORDER_EVENTS],
                      produces=[Topic.INVENTORY_EVENTS], handler=handler, stop=stop,
                      extra=[seed], ready=ready)
