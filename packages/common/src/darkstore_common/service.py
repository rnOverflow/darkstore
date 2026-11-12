"""Shared service runtime: schema, producer, topics, outbox relay, consumer, extra loops."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .consumer import ConsumerRunner, Handler
from .db import ServiceMeta, init_schema, make_engine, make_sessionmaker
from .kafka import ensure_topics, make_producer
from .outbox import OutboxRelay
from .settings import Settings
from .topics import Topic

Extra = Callable[[async_sessionmaker[AsyncSession], asyncio.Event], Awaitable[None]]


async def run_service(settings: Settings, *, meta: ServiceMeta, group: str,
                      consumes: Sequence[Topic], handler: Handler, stop: asyncio.Event,
                      produces: Sequence[Topic] = (), extra: Sequence[Extra] = (),
                      ready: asyncio.Event | None = None) -> None:
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    await init_schema(engine, meta)
    producer = await make_producer(settings.kafka_bootstrap)
    runner = ConsumerRunner(
        bootstrap=settings.kafka_bootstrap, group=group,
        topics=[settings.topic(t) for t in consumes], sessionmaker=sm, meta=meta,
        handler=handler, producer=producer, retry_delays=settings.retry_delays)
    names = [settings.topic(t) for t in {*consumes, *produces}] + runner.owned_topics()
    await ensure_topics(settings.kafka_bootstrap, names, settings.topic_partitions)
    relay = OutboxRelay(sm, meta, producer)
    tasks = [asyncio.create_task(c) for c in (runner.run(stop), relay.run(stop),
                                              *[e(sm, stop) for e in extra])]
    if ready:
        ready.set()
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await producer.stop()
        await engine.dispose()
