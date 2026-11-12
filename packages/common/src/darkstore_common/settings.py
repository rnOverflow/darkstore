from __future__ import annotations

from pydantic_settings import BaseSettings

from .topics import Topic


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://darkstore:darkstore-local@localhost:5432/darkstore"
    kafka_bootstrap: str = "localhost:19092"
    redis_url: str = "redis://localhost:6379/0"
    topic_prefix: str = "darkstore"
    topic_partitions: int = 3
    retry_delays: tuple[str, ...] = ("5s", "30s", "5m")
    pick_variable: bool = False   # True: lognormal pick time with picker queues (world.pick_minutes)
    pick_seconds: float = 5.0   # = 2.5 simulated minutes at the default 30x, matching the ETA world
    order_timeout_seconds: float = 600.0
    sweep_interval: float = 0.5
    seed_stock: bool = True
    http_port: int = 8001
    # rider simulation / dispatch
    riders: int = 100
    flaky_rate: float = 0.0           # fraction of riders that no-show their first assignment
    sim_speed: float = 30.0           # simulated seconds per real second
    sim_tick: float = 0.2             # real seconds between rider movement steps
    pickup_timeout_seconds: float = 90.0   # real seconds a rider has to pick up before reassignment
    rider_cooldown_seconds: float = 30.0
    match_interval: float = 0.2
    reconcile_grace_seconds: float = 30.0   # a Redis claim older than this with no DB backing is leaked
    # realtime gateway / tokens
    token_secret: str = ""                  # REQUIRED for tracking tokens (compose sets it from .env)
    tracking_token_ttl: int = 6 * 3600
    ws_auth_timeout: float = 5.0
    ws_heartbeat: float = 15.0
    ws_idle_timeout: float = 45.0
    ws_ack_window: int = 500                # unacknowledged messages before the server pauses sending
    ws_stall_timeout: float = 30.0          # paused this long without an ack -> client is cut off
    ws_allowed_origins: tuple[str, ...] = ()   # empty = any origin; compose sets the real page origin
    ws_max_conns_per_order: int = 5
    ws_max_conns_ops: int = 50
    ws_max_subscriptions: int = 20
    ws_max_frame_bytes: int = 4096
    # ETA serving
    eta_model_enabled: bool = True
    eta_model_dir: str = "models/eta/current"
    eta_reload_seconds: float = 10.0
    # observability (empty endpoint = telemetry off; compose points it at the collector)
    otel_endpoint: str = ""
    otel_sample_ratio: float = 1.0
    otel_metric_interval: float = 5.0
    # order simulator
    sim_orders_per_minute: float = 3.0    # base rate in simulated minutes, before rush multipliers
    sim_start_hour: float = 18.0
    order_api_url: str = "http://order:8001"

    def topic(self, t: Topic) -> str:
        return f"{self.topic_prefix}.{t.value}"
