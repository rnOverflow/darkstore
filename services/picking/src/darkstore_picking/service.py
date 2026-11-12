"""Picking: simulated picker latency via a DB-backed sweeper (survives restarts)."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from darkstore_common import telemetry
from darkstore_common.events import make_event
from darkstore_common.outbox import enqueue
from darkstore_common import world
from darkstore_common.loops import run_loop
from darkstore_common.riders import make_redis
from darkstore_common.simclock import load_clock
from darkstore_common.service import run_service
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from .models import META, picks


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    out = settings.topic(Topic.PICKING_EVENTS)
    rng = random.Random(7)
    redis = make_redis(settings.redis_url) if settings.pick_variable else None
    clock = await load_clock(redis, settings.sim_start_hour, settings.sim_speed) if redis else None

    async def handler(s, env):
        if env.event_type == "inventory.reserved":
            seconds = settings.pick_seconds
            if settings.pick_variable:      # same physics the ETA model is trained on
                queue = (await s.execute(select(func.count()).select_from(picks).where(
                    picks.c.store_id == env.payload["store_id"], picks.c.status == "started"))).scalar_one()
                day = clock.day()
                seconds = clock.real_seconds(world.pick_minutes(
                    rng, max(0, queue - world.PICKERS_PER_STORE), world.is_raining(day)))
            due = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            r = await s.execute(pg_insert(picks).values(
                order_id=env.order_id, store_id=env.payload["store_id"], status="started", due_at=due)
                .on_conflict_do_nothing().returning(picks.c.order_id))
            if r.first():
                await enqueue(s, META, out, make_event("picking.started", env.order_id,
                                                       store_id=env.payload["store_id"]))
        elif env.event_type == "order.cancelled":
            # upsert: a cancel that overtakes inventory.reserved leaves a tombstone, so the later
            # inventory.reserved finds an existing row and never starts picking a cancelled order
            now = datetime.now(timezone.utc)
            ins = pg_insert(picks).values(order_id=env.order_id, status="cancelled", due_at=now)
            await s.execute(ins.on_conflict_do_update(
                index_elements=["order_id"], set_={"status": "cancelled"},
                where=picks.c.status == "started"))

    async def sweeper(sm, stop):
        async def step():
            async with sm.begin() as s:
                rows = (await s.execute(
                    select(picks).where(picks.c.status == "started",
                                        picks.c.due_at <= datetime.now(timezone.utc))
                    .with_for_update(skip_locked=True))).all()
                for r in rows:
                    await s.execute(update(picks).where(picks.c.order_id == r.order_id)
                                    .values(status="completed", completed_at=datetime.now(timezone.utc)))
                    took = (datetime.now(timezone.utc) - r.started_at).total_seconds()
                    with telemetry.order_span("picking.complete", r.order_id, store_id=r.store_id,
                                              pick_seconds=took):
                        await enqueue(s, META, out, make_event(
                            "picking.completed", r.order_id, store_id=r.store_id, pick_seconds=took))
        await run_loop(stop, step, settings.sweep_interval, "picking-sweeper")

    try:
        await run_service(settings, meta=META, group="picking",
                          consumes=[Topic.INVENTORY_EVENTS, Topic.ORDER_EVENTS],
                          produces=[Topic.PICKING_EVENTS], handler=handler, stop=stop,
                          extra=[sweeper], ready=ready)
    finally:
        if redis:
            await redis.aclose()
