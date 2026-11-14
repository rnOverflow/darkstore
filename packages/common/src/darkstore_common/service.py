"""Shared service runtime: schema, producer, topics, outbox relay, consumer, extra loops."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .consumer import ConsumerRunner, Handler
from .db import ServiceMeta, init_schema, make_engine, make_sessionmaker
from .kafka import ensure_topics, make_producer
from . import retention
from .outbox import OutboxRelay
from .settings import Settings
from .topics import Topic

Extra = Callable[[async_sessionmaker[AsyncSession], asyncio.Event], Awaitable[None]]


async def run_service(settings: Settings, *, meta: ServiceMeta, group: str,
                      consumes: Sequence[Topic], handler: Handler | None, stop: asyncio.Event,
                      produces: Sequence[Topic] = (), extra: Sequence[Extra] = (),
                      ready: asyncio.Event | None = None, dedupe: bool = True) -> None:
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    await init_schema(engine, meta)
    producer = await make_producer(settings.kafka_bootstrap)
    runner = ConsumerRunner(
        bootstrap=settings.kafka_bootstrap, group=group,
        topics=[settings.topic(t) for t in consumes], sessionmaker=sm, meta=meta,
        handler=handler, producer=producer, retry_delays=settings.retry_delays, dedupe=dedupe)
    names = [settings.topic(t) for t in {*consumes, *produces}] + runner.owned_topics()
    await ensure_topics(settings.kafka_bootstrap, names, settings.topic_partitions)
    relay = OutboxRelay(sm, meta, producer)
    loops = [relay.run(stop), retention.loop(settings, meta)(sm, stop), *[e(sm, stop) for e in extra]]
    if consumes:  # producer-only services (e.g. rider-sim) have nothing to consume
        loops.insert(0, runner.run(stop))
    tasks = [asyncio.create_task(c) for c in loops]
    if ready:
        ready.set()
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await producer.stop()
        await engine.dispose()
