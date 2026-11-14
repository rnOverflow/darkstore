"""Order simulator: customers across the city, demand following rush-hour curves."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid

import httpx

from darkstore_common.city import STORES, nearest_store, random_point
from darkstore_common.riders import make_redis
from darkstore_common.settings import Settings
from darkstore_common.simclock import load_clock

from .demand import orders_per_minute

log = logging.getLogger("darkstore.simulator")
TICK = 0.1
N_SKUS = 20  # matches the inventory seed (sku-0 .. sku-19)


def poisson(rng: random.Random, lam: float) -> int:
    """Knuth's algorithm; lam is per-tick so it stays small."""
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def new_order(rng: random.Random) -> dict:
    lat, lng = random_point(rng)
    items = [{"sku": f"sku-{s}", "qty": rng.randint(1, 3)}
             for s in rng.sample(range(N_SKUS), rng.randint(1, 4))]
    return {"customer_id": f"cust-{rng.randint(1, 5000)}", "store_id": nearest_store(lat, lng),
            "items": items, "lat": lat, "lng": lng}


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    rng = random.Random(1234)
    placed, failed = 0, 0
    redis = make_redis(settings.redis_url)
    clock = await load_clock(redis, settings.sim_start_hour, settings.sim_speed)
    sem = asyncio.Semaphore(50)
    tasks: set[asyncio.Task] = set()

    async with httpx.AsyncClient(base_url=settings.order_api_url, timeout=10) as client:
        session = {"token": None, "at": 0.0}

        async def bearer() -> dict[str, str]:
            """One guest session for all simulated orders, renewed well before it expires."""
            if session["token"] is None or time.time() - session["at"] > settings.guest_token_ttl - 600:
                r = await client.post("/sessions")
                r.raise_for_status()
                session.update(token=r.json()["token"], at=time.time())
            return {"Authorization": f"Bearer {session['token']}"}

        async def place():
            nonlocal placed, failed
            async with sem:
                try:
                    r = await client.post("/orders", json=new_order(rng),
                                          headers={**await bearer(), "Idempotency-Key": uuid.uuid4().hex})
                    placed += r.status_code == 201
                    failed += r.status_code != 201
                except httpx.HTTPError:
                    failed += 1

        last_report = time.time()
        if ready:
            ready.set()
        while not stop.is_set():
            hour = clock.hour()
            per_real_s = orders_per_minute(hour, settings.sim_orders_per_minute) \
                * settings.sim_speed / 60.0
            for _ in range(poisson(rng, per_real_s * TICK)):
                t = asyncio.create_task(place())
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            if time.time() - last_report > 10:
                last_report = time.time()
                log.info("sim hour %.1f: %.2f orders/s target, placed=%d failed=%d stores=%d",
                         hour, per_real_s, placed, failed, len(STORES))
            await asyncio.sleep(TICK)
        await asyncio.gather(*tasks, return_exceptions=True)
    await redis.aclose()
