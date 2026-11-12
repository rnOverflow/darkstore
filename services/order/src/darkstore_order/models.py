from sqlalchemy import JSON, Column, DateTime, String, Table, UniqueConstraint, func

from darkstore_common.db import service_meta

META = service_meta("orders")
orders = Table(
    "orders", META.metadata,
    Column("order_id", String, primary_key=True),
    Column("customer_id", String, nullable=False),
    Column("store_id", String, nullable=False),
    Column("items", JSON, nullable=False),
    Column("status", String, nullable=False),
    Column("cancel_reason", String),
    Column("rider_id", String),  # the rider dispatch assigned; rider.* events must match it
    Column("idempotency_key", String),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("customer_id", "idempotency_key", name="uq_orders_customer_idem"),
)
