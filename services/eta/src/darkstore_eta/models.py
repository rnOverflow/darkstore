from sqlalchemy import Column, DateTime, Float, Integer, String, Table

from darkstore_common.db import service_meta

META = service_meta("eta")

# One row per order: what we knew when, plus the order-time context the model was trained on.
order_state = Table(
    "order_state", META.metadata,
    Column("order_id", String, primary_key=True),
    Column("store_id", String, nullable=False),
    Column("cust_lat", Float, nullable=False), Column("cust_lng", Float, nullable=False),
    Column("cust_cell", String, nullable=False),
    Column("status", String, nullable=False),          # active | delivered | cancelled
    Column("stage", Integer, nullable=False),          # 0 created 1 picking 2 assigned 3 out_for_delivery
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("pick_started_at", DateTime(timezone=True)),
    Column("pick_done_at", DateTime(timezone=True)),
    Column("assigned_at", DateTime(timezone=True)),
    Column("picked_up_at", DateTime(timezone=True)),
    # order-time context (constant for the life of the order, as in the training data)
    Column("active_orders", Integer), Column("queue_depth", Integer),
    Column("idle_near", Integer), Column("est_rider_km", Float),
    Column("rider_km", Float),                          # the real rider's distance, once assigned
    Column("first_p50", Float), Column("first_p90", Float),
    Column("last_p50", Float), Column("last_p90", Float),
    Column("model_version", String),
    Column("first_source", String), Column("first_model_version", String),
    Column("last_event_at", DateTime(timezone=True)),   # newest event published for: drop stale ones
)

# Predicted vs actual for every delivered order: the raw material for accuracy/drift dashboards.
outcomes = Table(
    "outcomes", META.metadata,
    Column("order_id", String, primary_key=True),
    Column("actual_min", Float, nullable=False),        # simulated minutes, order -> delivered
    Column("first_p50", Float), Column("first_p90", Float),
    Column("source", String), Column("model_version", String),
    Column("delivered_at", DateTime(timezone=True), nullable=False),
)
