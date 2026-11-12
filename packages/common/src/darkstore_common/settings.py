from __future__ import annotations

from pydantic_settings import BaseSettings

from .topics import Topic


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://darkstore:darkstore-local@localhost:5432/darkstore"
    kafka_bootstrap: str = "localhost:19092"
    topic_prefix: str = "darkstore"
    topic_partitions: int = 3
    retry_delays: tuple[str, ...] = ("5s", "30s", "5m")
    pick_seconds: float = 3.0
    order_timeout_seconds: float = 600.0
    sweep_interval: float = 0.5
    seed_stock: bool = True
    http_port: int = 8001

    def topic(self, t: Topic) -> str:
        return f"{self.topic_prefix}.{t.value}"
