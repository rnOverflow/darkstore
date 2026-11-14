"""Rider simulator: GPS movement for every rider and the pickup/delivery lifecycle."""
from __future__ import annotations

import asyncio
import logging
import random
import time

from darkstore_common.city import STORES
from darkstore_common import telemetry
from darkstore_common.events import make_event
from darkstore_common.geo import ROAD_FACTOR, return_to_base, step_towards
from darkstore_common.kafka import make_producer
from darkstore_common.loops import run_loop
from darkstore_common.outbox import enqueue
from darkstore_common.riders import RiderIndex, make_redis
from darkstore_common.service import run_service
from darkstore_common.simclock import load_clock
from darkstore_common.world import is_raining, speed_multiplier
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from .models import META

log = logging.getLogger("darkstore.rider_sim")
POSITION_EVERY = 1.0  # real seconds between position events per busy rider


def home_of(rider_id: str) -> tuple[float, float]:
    """A rider's base: fleet spread evenly over the stores, derived from the id (no extra state)."""
    stores = list(STORES.values())
    return stores[int(rider_id.rsplit("-", 1)[1]) % len(stores)]


async def seed_riders(index: RiderIndex, settings: Settings, rng: random.Random) -> int:
    """Create the rider fleet once (idle, clustered around dark stores).

    Rider ids are deterministic and add_rider is idempotent, so a crash part-way through simply
    re-runs on restart; the `seeded` marker is only written once the whole fleet exists.
    """
    if await index.r.get(f"{index.p}seeded"):
        return 0
    for i in range(settings.riders):
        lat, lng = home_of(f"rider-{i:04d}")
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
    last_pub: dict[str, float] = {}

    async def loop(sm, stop):
        producer = await make_producer(settings.kafka_bootstrap)
        dispatch_topic = settings.topic(Topic.DISPATCH_EVENTS)
        pos_topic = settings.topic(Topic.RIDER_POSITIONS)
        last_step = time.time()
        MAX_DT = 5.0   # real seconds: after a stall riders catch up a little, they do not teleport
        clock = await load_clock(redis, settings.sim_start_hour, settings.sim_speed)
        await seed_riders(index, settings, rng)

        async def emit(order_id: str, event_type: str, rider_id: str) -> None:
            with telemetry.order_span(f"rider.{event_type.split('.')[1]}", order_id, rider_id=rider_id):
                async with sm.begin() as s:
                    await enqueue(s, META, dispatch_topic, make_event(event_type, order_id,
                                                                       rider_id=rider_id))

        async def advance(rid: str, h: dict[str, str], hour: float, raining: bool, dt_h: float) -> None:
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
            mult = speed_multiplier(hour, raining, h["cell"])  # traffic x rain x slow neighbourhoods
            km = float(h["speed"]) * mult / ROAD_FACTOR * dt_h   # dt_h = MEASURED time since last step
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

        async def tick_rider(rid: str, h: dict[str, str], hour: float, raining: bool, dt_h: float) -> None:
            status = h.get("status")
            if status == "assigned":
                await advance(rid, h, hour, raining, dt_h)
            elif status == "cooldown":
                if await index.recover_one(rid):
                    await index.clear_noshow(rid)  # back to normal after the bench
            elif status == "idle":
                lat, lng = float(h["lat"]), float(h["lng"])
                km = float(h["speed"]) * speed_multiplier(hour, raining, h["cell"]) / ROAD_FACTOR * dt_h
                nlat, nlng = return_to_base(lat, lng, *home_of(rid), km)   # cycle back to the home store
                if (nlat, nlng) != (lat, lng):
                    await index.move(rid, nlat, nlng)
                elif rng.random() < 0.05:   # at base: drift a little
                    await index.move(rid, lat + rng.uniform(-0.0004, 0.0004),
                                     lng + rng.uniform(-0.0004, 0.0004))

        async def step():
            nonlocal last_step
            ids = await index.all_ids()
            now = time.time()
            # movement uses the time that ACTUALLY passed, not the nominal tick: a slow step over many
            # riders must not make the simulated city run slower than the simulated clock
            dt_h = min(now - last_step, MAX_DT) * settings.sim_speed / 3600.0
            last_step = now
            hour, raining = clock.hour(now), is_raining(clock.day(now))
            for rid, h in zip(ids, await index.get_many(ids)):
                try:
                    await tick_rider(rid, h, hour, raining, dt_h)
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
