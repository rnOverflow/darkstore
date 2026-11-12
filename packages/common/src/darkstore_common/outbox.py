"""Transactional outbox: write with the state change, relay to Kafka at-least-once."""
from __future__ import annotations

import asyncio
import logging

from aiokafka import AIOKafkaProducer
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import telemetry
from .db import ServiceMeta
from .envelope import Envelope


log = logging.getLogger("darkstore.outbox")


async def enqueue(session: AsyncSession, meta: ServiceMeta, topic: str, env: Envelope,
                  headers: dict[str, str] | None = None) -> None:
    await session.execute(insert(meta.outbox).values(
        topic=topic, key=env.order_id, event_id=env.event_id, body=env.model_dump_json(),
        headers={**telemetry.inject_headers(env.order_id), **(headers or {})}))


class OutboxRelay:
    """Run ONE relay per service: ordering across batches relies on a single publisher."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], meta: ServiceMeta,
                 producer: AIOKafkaProducer, batch: int = 200, poll: float = 0.1):
        self.sm, self.meta, self.producer, self.batch, self.poll = sessionmaker, meta, producer, batch, poll

    async def run_once(self) -> int:
        ob = self.meta.outbox
        async with self.sm.begin() as s:
            rows = (await s.execute(select(ob).where(ob.c.published_at.is_(None))
                                    .order_by(ob.c.id).limit(self.batch)
                                    .with_for_update(skip_locked=True))).all()
            for r in rows:
                await self.producer.send_and_wait(
                    r.topic, value=r.body.encode(), key=r.key.encode(),
                    headers=[(k, v.encode()) for k, v in (r.headers or {}).items()])
            if rows:
                await s.execute(update(ob).where(ob.c.id.in_([r.id for r in rows]))
                                .values(published_at=func.now()))
        return len(rows)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                n = await self.run_once()
            except Exception:  # noqa: BLE001 - broker/DB blip: rows stay unpublished, retry
                log.exception("outbox relay iteration failed; retrying")
                n = 0
                await asyncio.sleep(1)
            if n == 0:
                await asyncio.sleep(self.poll)
