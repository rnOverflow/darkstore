"""Bounded storage: purge published outbox rows and old processed_events.

processed_events must outlive the longest possible redelivery (retry topics reach 5 minutes, a DLQ replay
can be later) or a duplicate would be applied twice, so it keeps 24 h by default; published outbox rows
are only an audit trail and keep 1 h. Unpublished outbox rows are NEVER deleted.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import ServiceMeta
from .settings import Settings

log = logging.getLogger("darkstore.retention")
BATCH = 5000


async def purge(sm: async_sessionmaker[AsyncSession], meta: ServiceMeta, *, outbox_s: float,
                processed_s: float) -> tuple[int, int]:
    """Delete in small batches (short transactions, no long locks). Returns (outbox, processed) deleted."""
    schema = meta.metadata.schema   # validated against [a-z_]+ in service_meta
    plans = (
        (f'DELETE FROM "{schema}".outbox WHERE id IN (SELECT id FROM "{schema}".outbox '
         f"WHERE published_at IS NOT NULL AND published_at < now() - make_interval(secs => :age) "
         f"ORDER BY id LIMIT {BATCH})", outbox_s),
        (f'DELETE FROM "{schema}".processed_events WHERE ctid IN (SELECT ctid FROM "{schema}".processed_events '
         f"WHERE processed_at < now() - make_interval(secs => :age) LIMIT {BATCH})", processed_s),
    )
    totals = []
    for sql, age in plans:
        total = 0
        while True:
            async with sm.begin() as s:
                n = (await s.execute(text(sql), {"age": age})).rowcount
            total += n
            if n < BATCH:
                break
        totals.append(total)
    return totals[0], totals[1]


def loop(settings: Settings, meta: ServiceMeta):
    """An `extra` for run_service. Disabled when retention_interval_s <= 0."""
    async def _extra(sm, stop):
        from .loops import run_loop

        async def step():
            o, p = await purge(sm, meta, outbox_s=settings.outbox_retention_s,
                               processed_s=settings.processed_retention_s)
            if o or p:
                log.info("retention: purged %d outbox rows, %d processed_events", o, p)
        if settings.retention_interval_s > 0:
            await run_loop(stop, step, settings.retention_interval_s, "retention")
        else:
            await stop.wait()
    return _extra
