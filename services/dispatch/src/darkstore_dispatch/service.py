from __future__ import annotations

import asyncio

from darkstore_common.loops import run_loop
from darkstore_common.riders import RiderIndex, make_redis
from darkstore_common.service import run_service
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from . import logic
from .models import META


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    redis = make_redis(settings.redis_url)
    index = RiderIndex(redis)

    async def handler(session, env):
        await logic.handle(session, index, env)

    async def matcher(sm, stop):
        async def step():
            await logic.match_pending(sm, index, settings)
        await run_loop(stop, step, settings.match_interval, "dispatch-matcher")

    async def timeouts(sm, stop):
        async def step():
            async with sm.begin() as s:
                await logic.reassign_timed_out(s, index, settings)
            async with sm.begin() as s:
                await logic.reconcile(s, index, settings.reconcile_grace_seconds)
        await run_loop(stop, step, 1.0, "dispatch-timeouts")

    try:
        await run_service(
            settings, meta=META, group="dispatch",
            consumes=[Topic.ORDER_EVENTS, Topic.PICKING_EVENTS, Topic.DISPATCH_EVENTS],
            produces=[Topic.DISPATCH_EVENTS], handler=handler, stop=stop,
            extra=[matcher, timeouts], ready=ready)
    finally:
        await redis.aclose()
