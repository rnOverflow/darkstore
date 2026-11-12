from __future__ import annotations

import asyncio
import time

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import KafkaError, TopicAlreadyExistsError


async def make_producer(bootstrap: str) -> AIOKafkaProducer:
    p = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", enable_idempotence=True,
                         linger_ms=5)
    await p.start()
    return p


async def ensure_topics(bootstrap: str, names: list[str], partitions: int = 3,
                        timeout: float = 30.0) -> None:
    """Create topics (idempotent, safe when several services race) and wait until all are visible."""
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    want, deadline, last_err = set(names), time.time() + timeout, None
    try:
        while True:
            try:
                await admin.create_topics([NewTopic(n, partitions, 1) for n in sorted(want)])
            except TopicAlreadyExistsError:
                pass  # another service created some/all of them
            except KafkaError as e:
                last_err = e  # transient (e.g. controller busy); verified below
            existing = set(await admin.list_topics())
            if want <= existing:
                return
            if time.time() > deadline:
                raise TimeoutError(f"topics not created: {sorted(want - existing)}; last error: {last_err!r}")
            await asyncio.sleep(0.3)
    finally:
        await admin.close()
