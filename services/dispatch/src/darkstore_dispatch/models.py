from sqlalchemy import (BigInteger, Column, DateTime, Float, Integer, String, Table, func)

from darkstore_common.db import service_meta

META = service_meta("dispatch")

# One row per order. status: awaiting_pick | pending | assigned | picked_up | delivered | cancelled
assignments = Table(
    "assignments", META.metadata,
    Column("order_id", String, primary_key=True),
    Column("store_id", String),
    Column("store_lat", Float), Column("store_lng", Float),
    Column("dest_lat", Float), Column("dest_lng", Float),
    Column("status", String, nullable=False),
    Column("rider_id", String),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("ready_at", DateTime(timezone=True)),
    Column("assigned_at", DateTime(timezone=True)),
    Column("pickup_deadline", DateTime(timezone=True)),
)

# Append-only audit trail; the gate test replays it to prove no rider ever held two orders.
assignment_log = Table(
    "assignment_log", META.metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("order_id", String, nullable=False),
    Column("rider_id", String, nullable=False),
    # assigned | timed_out | cancel_release | picked_up | delivered
    Column("action", String, nullable=False),
    Column("at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)
