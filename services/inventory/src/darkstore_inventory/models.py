from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Integer, String, Table, func

from darkstore_common.db import service_meta

META = service_meta("inventory")
stock = Table(
    "stock", META.metadata,
    Column("store_id", String, primary_key=True),
    Column("sku", String, primary_key=True),
    Column("available", Integer, nullable=False),
    CheckConstraint("available >= 0", name="ck_stock_nonneg"),
)
reservations = Table(
    "reservations", META.metadata,
    Column("order_id", String, primary_key=True),
    Column("store_id", String),  # NULL for a cancel tombstone
    Column("items", JSON, nullable=False),
    Column("status", String, nullable=False),  # reserved | rejected | released
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)
