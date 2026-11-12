"""Rider simulator: GPS movement for every rider and the pickup/delivery lifecycle."""
from __future__ import annotations

import asyncio
import logging
import random
import time

from darkstore_common.city import STORES, sim_hour, traffic_factor
from darkstore_common.events import make_event
from darkstore_common.geo import ROAD_FACTOR, step_towards
from darkstore_common.kafka import make_producer
from darkstore_common.loops import run_loop
from darkstore_common.outbox import enqueue
from darkstore_common.riders import RiderIndex, make_redis
from darkstore_common.service import run_service
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from .models import META

log = logging.getLogger("darkstore.rider_sim")
POSITION_EVERY = 1.0  # real seconds between position events per busy rider


async def seed_riders(index: RiderIndex, settings: Settings, rng: random.Random) -> int:
    """Create the rider fleet once (idle, clustered around dark stores).

    Rider ids are deterministic and add_rider is idempotent, so a crash part-way through simply
    re-runs on restart; the `seeded` marker is only written once the whole fleet exists.
    """
    if await index.r.get(f"{index.p}seeded"):
        return 0
    stores = list(STORES.values())
    for i in range(settings.riders):
        lat, lng = rng.choice(stores)
        await index.add_rider(
            f"rider-{i:04d}", lat + rng.uniform(-0.012, 0.012), lng + rng.uniform(-0.012, 0.012),
            rating=round(rng.uniform(3.5, 5.0), 2), speed_kmh=round(rng.uniform(16, 24), 1),
            flaky=rng.random() < settings.flaky_rate)
    await index.r.set(f"{index.p}seeded", "1")
    return settings.riders


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    redis = make_redis(settings.redis_url)
    index = RiderIndex(redis)
    rng = random.Random(42)
    t0 = time.time()
    last_pub: dict[str, float] = {}

    async def loop(sm, stop):
        producer = await make_producer(settings.kafka_bootstrap)
        dispatch_topic = settings.topic(Topic.DISPATCH_EVENTS)
        pos_topic = settings.topic(Topic.RIDER_POSITIONS)
        dt_h = settings.sim_tick * settings.sim_speed / 3600.0
        await seed_riders(index, settings, rng)

        async def emit(order_id: str, event_type: str, rider_id: str) -> None:
            async with sm.begin() as s:
                await enqueue(s, META, dispatch_topic, make_event(event_type, order_id,
                                                                   rider_id=rider_id))

        async def advance(rid: str, h: dict[str, str], tf: float) -> None:
            """One tick for a rider holding an order. Every phase is retryable: if we crash or an
            emit fails, the phase is unchanged and the next tick simply tries again (dispatch and
            order-service ignore duplicate/stale events)."""
            oid, phase = h["order_id"], h.get("phase", "to_store")
            if h.get("flaky") == "1":  # no-show: ignore this one assignment, behave afterwards
                await index.mark_noshow(rid, oid)
                return
            if h.get("noshow") == oid or phase == "done":
                return  # 'done': delivered; dispatch frees the rider when it processes the event
            if phase == "at_store":
                await emit(oid, "rider.picked_up", rid)
                await index.phase_cas(rid, oid, "at_store", "to_customer")
                return
            if phase == "at_customer":
                await emit(oid, "rider.delivered", rid)
                await index.phase_cas(rid, oid, "at_customer", "done")
                return
            tgt = (float(h["store_lat"]), float(h["store_lng"])) if phase == "to_store" \
                else (float(h["dest_lat"]), float(h["dest_lng"]))
            km = float(h["speed"]) * tf / ROAD_FACTOR * dt_h
            lat, lng, arrived = step_towards(float(h["lat"]), float(h["lng"]), *tgt, km)
            await index.move(rid, lat, lng)
            now = time.time()
            if now - last_pub.get(rid, 0) >= POSITION_EVERY:
                last_pub[rid] = now
                ev = make_event("rider.position", oid, rider_id=rid, lat=lat, lng=lng)
                await producer.send(pos_topic, value=ev.model_dump_json().encode(),
                                    key=ev.partition_key())
            if arrived:
                nxt = "at_store" if phase == "to_store" else "at_customer"
                await index.phase_cas(rid, oid, phase, nxt)

        async def tick_rider(rid: str, h: dict[str, str], tf: float) -> None:
            status = h.get("status")
            if status == "assigned":
                await advance(rid, h, tf)
            elif status == "cooldown":
                if await index.recover_one(rid):
                    await index.clear_noshow(rid)  # back to normal after the bench
            elif status == "idle" and rng.random() < 0.05:  # idle riders drift a little
                await index.move(rid, float(h["lat"]) + rng.uniform(-0.0004, 0.0004),
                                 float(h["lng"]) + rng.uniform(-0.0004, 0.0004))

        async def step():
            ids = await index.all_ids()
            tf = traffic_factor(sim_hour(settings.sim_start_hour, settings.sim_speed,
                                         time.time() - t0))
            for rid, h in zip(ids, await index.get_many(ids)):
                try:
                    await tick_rider(rid, h, tf)
                except Exception:  # noqa: BLE001 - one rider's failure must not stall the fleet
                    log.exception("rider %s tick failed; will retry", rid)

        try:
            await run_loop(stop, step, settings.sim_tick, "rider-sim")
        finally:
            await producer.stop()

    try:
        await run_service(settings, meta=META, group="rider_sim", consumes=[], handler=None,
                          produces=[Topic.DISPATCH_EVENTS, Topic.RIDER_POSITIONS], stop=stop,
                          extra=[loop], ready=ready)
    finally:
        await redis.aclose()
