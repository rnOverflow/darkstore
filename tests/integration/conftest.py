"""Real Postgres + Redpanda via testcontainers (session scoped), isolated per test by topic prefix."""
from __future__ import annotations

import socket
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.postgres import PostgresContainer

from darkstore_common.settings import Settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def kafka_bootstrap():
    port = _free_port()
    c = (DockerContainer("redpandadata/redpanda:v24.2.7")
         .with_bind_ports(port, port)
         .with_kwargs(mem_limit="768m")
         .with_command(
             f"redpanda start --mode=dev-container --smp=1 --memory=512M --reserve-memory=0M "
             f"--overprovisioned --kafka-addr=PLAINTEXT://0.0.0.0:{port} "
             f"--advertise-kafka-addr=PLAINTEXT://localhost:{port}"))
    with c:
        wait_for_logs(c, "Successfully started Redpanda", timeout=90)
        # dev-sized broker: 256 KiB/partition instead of 4 MiB so retry/DLQ topics fit
        code, out = c.exec("rpk cluster config set topic_memory_per_partition 262144")
        assert code == 0, out
        yield f"localhost:{port}"


@pytest.fixture(scope="session")
def redis_url():
    c = DockerContainer("redis:7-alpine").with_exposed_ports(6379)
    with c:
        wait_for_logs(c, "Ready to accept connections", timeout=30)
        yield f"redis://{c.get_container_host_ip()}:{c.get_exposed_port(6379)}/0"


@pytest.fixture
async def index(redis_url):
    """A RiderIndex on a private key prefix (isolated per test)."""
    from darkstore_common.riders import RiderIndex, make_redis
    r = make_redis(redis_url, max_connections=600)  # tests fire hundreds of concurrent calls
    yield RiderIndex(r, prefix=f"t{uuid.uuid4().hex[:8]}:")
    await r.aclose()


async def _drop_topics(bootstrap: str, prefix: str) -> None:
    from aiokafka.admin import AIOKafkaAdminClient
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        doomed = [t for t in await admin.list_topics() if t.startswith(prefix + ".")]
        if doomed:
            await admin.delete_topics(doomed)
    finally:
        await admin.close()


@pytest.fixture
async def env(pg_url, kafka_bootstrap, redis_url) -> Settings:
    from darkstore_common.riders import make_redis
    rds = make_redis(redis_url)
    await rds.flushall()  # rider index lives under the default "ds:" prefix in services
    await rds.aclose()
    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        for schema in ("orders", "inventory", "picking", "dispatch", "rider_sim", "eta", "ctest"):
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await engine.dispose()
    settings = Settings(database_url=pg_url, kafka_bootstrap=kafka_bootstrap,
                    topic_prefix=f"t{uuid.uuid4().hex[:8]}", retry_delays=("1s", "2s"),
                    pick_seconds=0.3, sweep_interval=0.1, topic_partitions=3, seed_stock=False, redis_url=redis_url)
    yield settings
    await _drop_topics(kafka_bootstrap, settings.topic_prefix)  # keep the shared broker under its partition cap
