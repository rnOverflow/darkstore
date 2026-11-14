from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

import httpx
from darkstore_common.envelope import Envelope
from darkstore_common.tokens import sign


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


TEST_SECRET = "t" * 32


class AutoAuth(httpx.Auth):
    """Test client auth: POST /orders as a guest session, every other call as ops (which may read/cancel any
    order). An explicit Authorization header always wins, so auth tests can send the exact token they want."""

    def __init__(self, secret: str, customer: str = "c1"):
        self.secret, self.customer = secret, customer

    def auth_flow(self, request):
        if "authorization" not in request.headers:
            if request.method == "POST" and request.url.path == "/orders":
                tok = sign(self.secret, role="guest", customer_id=self.customer, ttl_s=600)
            else:
                tok = sign(self.secret, role="ops", ttl_s=600)
            request.headers["Authorization"] = f"Bearer {tok}"
        yield request
