from sqlalchemy import Column, DateTime, String, Table, func

from darkstore_common.db import service_meta

META = service_meta("picking")
picks = Table(
    "picks", META.metadata,
    Column("order_id", String, primary_key=True),
    Column("store_id", String),  # NULL for a cancel tombstone
    Column("status", String, nullable=False),  # started | completed | cancelled
    Column("started_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),    # real time the pick finished: ops p95 pick time
)
