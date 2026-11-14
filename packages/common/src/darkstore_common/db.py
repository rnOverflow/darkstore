"""Per-service schema: domain tables live with the service; outbox + processed_events are shared shape."""
from __future__ import annotations

import re
from typing import NamedTuple

from sqlalchemy import (JSON, BigInteger, Column, DateTime, Index, MetaData, String, Table, Text,
                        func, text)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class ServiceMeta(NamedTuple):
    metadata: MetaData
    outbox: Table
    processed: Table


def service_meta(schema: str) -> ServiceMeta:
    if not re.fullmatch(r"[a-z_]+", schema):  # schema names are interpolated into DDL
        raise ValueError(f"bad schema name {schema!r}")
    md = MetaData(schema=schema)
    outbox = Table(
        "outbox", md,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("topic", String, nullable=False),
        Column("key", String, nullable=False),
        Column("event_id", String, nullable=False),
        Column("body", Text, nullable=False),
        Column("headers", JSON, nullable=False, default=dict),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column("published_at", DateTime(timezone=True), nullable=True),
        Index(f"ix_{schema}_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )
    processed = Table(
        "processed_events", md,
        Column("consumer", String, primary_key=True),
        Column("event_id", String, primary_key=True),
        Column("processed_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    return ServiceMeta(md, outbox, processed)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_size=10, max_overflow=10)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_schema(engine: AsyncEngine, meta: ServiceMeta) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(42)"))  # serialize concurrent DDL
        # CREATE SCHEMA IF NOT EXISTS still demands CREATE on the database even when the schema exists, and a
        # least-privilege service role (own schema only) does not have it: look first, create only if missing
        exists = (await conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :n"),
                                     {"n": meta.metadata.schema})).first()
        if exists is None:
            await conn.execute(text(f'CREATE SCHEMA "{meta.metadata.schema}"'))
        await conn.run_sync(meta.metadata.create_all)
