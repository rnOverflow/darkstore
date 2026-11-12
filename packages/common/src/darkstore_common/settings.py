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
    pick_seconds: float = 3.0
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
    # order simulator
    sim_orders_per_minute: float = 3.0    # base rate in simulated minutes, before rush multipliers
    sim_start_hour: float = 18.0
    order_api_url: str = "http://order:8001"

    def topic(self, t: Topic) -> str:
        return f"{self.topic_prefix}.{t.value}"
