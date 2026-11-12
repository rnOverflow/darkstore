from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from darkstore_common.envelope import Envelope


async def publish(bootstrap: str, topic: str, env: Envelope, headers=None) -> None:
    p = AIOKafkaProducer(bootstrap_servers=bootstrap)
    await p.start()
    try:
        await p.send_and_wait(topic, value=env.model_dump_json().encode(), key=env.partition_key(),
                              headers=[(k, v.encode()) for k, v in (headers or {}).items()])
    finally:
        await p.stop()


async def read_all(bootstrap: str, topic: str, wait: float = 6.0, want: int = 1) -> list:
    """Read messages from topic start until `want` messages or timeout."""
    c = AIOKafkaConsumer(topic, bootstrap_servers=bootstrap, auto_offset_reset="earliest",
                         group_id=None)
    await c.start()
    out, deadline = [], time.time() + wait
    try:
        while time.time() < deadline and len(out) < want:
            for msgs in (await c.getmany(timeout_ms=300)).values():
                out.extend(msgs)
    finally:
        await c.stop()
    return out


async def until(predicate, timeout: float = 30.0, interval: float = 0.2):
    """Poll an async predicate until truthy; raise with last value on timeout."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met in {timeout}s; last={last!r}")


@asynccontextmanager
async def running(coro_factory):
    """Run a service coroutine (taking a stop Event) in the background for the block."""
    stop = asyncio.Event()
    task = asyncio.create_task(coro_factory(stop))
    try:
        yield stop
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)
