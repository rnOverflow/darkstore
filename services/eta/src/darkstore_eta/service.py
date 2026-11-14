"""ETA service: re-predicts on every order state change and publishes `eta.updated`."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from darkstore_common import telemetry
from darkstore_common.city import STORES
from darkstore_common.envelope import Envelope
from darkstore_common.events import make_event
from darkstore_common.geo import cell_of, haversine_km
from darkstore_common.loops import run_loop
from darkstore_common.outbox import enqueue
from darkstore_common.riders import RiderIndex, make_redis
from darkstore_common.service import run_service
from darkstore_common.settings import Settings
from darkstore_common.simclock import SimClock, load_clock
from darkstore_common.topics import Topic
from darkstore_common.world import PICKERS_PER_STORE, is_raining

from .models import META, order_state, outcomes
from .predictor import EtaPredictor
from .simulate import STAGES

log = logging.getLogger("darkstore.eta")
DEFAULT_EST_KM = 2.5


class EtaHandler:
    def __init__(self, settings: Settings, predictor: EtaPredictor, index: RiderIndex, clock: SimClock):
        self.st, self.predictor, self.index, self.clock = settings, predictor, index, clock

    async def _fleet_context(self, lat: float, lng: float) -> tuple[int, float]:
        """Idle riders near the store and the nearest one's distance (defaults if Redis is down)."""
        try:
            cands = await self.index.search(lat, lng, want=5, max_k=4)
        except Exception:  # noqa: BLE001 - ETA must still work without fleet context
            return 0, DEFAULT_EST_KM
        if not cands:
            return 0, DEFAULT_EST_KM
        # cap at the training range: the generator never sees more than ~12 idle riders nearby
        return min(len(cands), 12), min(haversine_km(c.lat, c.lng, lat, lng) for c in cands)

    async def handle(self, s: AsyncSession, env: Envelope) -> None:
        et, oid, ts = env.event_type, env.order_id, env.occurred_at
        if et == "order.created":
            await self._create(s, env)
        row = (await s.execute(select(order_state).where(order_state.c.order_id == oid)
                               .with_for_update())).first()
        if row is None:
            raise LookupError(f"order.created for {oid} not seen yet")  # cross-topic: retry
        if row.status != "active":
            return
        if et == "order.cancelled":
            await s.execute(update(order_state).where(order_state.c.order_id == oid)
                            .values(status="cancelled"))
            return
        if et == "order.delivered":
            actual = self.clock.sim_minutes((ts - row.created_at).total_seconds())
            self._score(row, actual)
            await s.execute(pg_insert(outcomes).values(
                order_id=oid, actual_min=actual, first_p50=row.first_p50, first_p90=row.first_p90,
                source=row.first_source, model_version=row.first_model_version,
                delivered_at=ts).on_conflict_do_nothing())
            await s.execute(update(order_state).where(order_state.c.order_id == oid)
                            .values(status="delivered"))
            return
        changes: dict = {}
        stage = row.stage
        if row.last_event_at is not None and ts < row.last_event_at and et != "order.created":
            return                                  # older than what we already published: stale
        if et == "picking.started" and row.pick_started_at is None:
            changes.update(pick_started_at=ts, stage=max(stage, 1))
        elif et == "picking.completed" and row.pick_done_at is None:
            changes.update(pick_done_at=ts, stage=max(stage, 1))
        elif et == "rider.assigned":
            changes.update(assigned_at=ts, stage=max(stage, 2))
            if stage < 3:                           # a late reassignment must not rewrite the km
                changes["rider_km"] = env.payload.get("rider_to_store_km") or None
        elif et == "rider.picked_up":
            changes.update(picked_up_at=ts, stage=max(stage, 3))
        elif et != "order.created":
            return
        if not changes and et != "order.created":
            return                                  # duplicate: nothing new to predict from
        changes["last_event_at"] = ts
        await s.execute(update(order_state).where(order_state.c.order_id == oid).values(**changes))
        row = SimpleNamespace(**{**row._mapping, **changes})
        await self._publish(s, row, ts)

    @staticmethod
    def _score(row, actual: float) -> None:
        """Live ETA accuracy: the FIRST promised ETA against what happened (simulated minutes)."""
        if row.first_p50 is None:
            return
        ins = telemetry.instruments()
        src = {"source": row.first_source or "unknown"}
        ins.eta_outcomes.add(1, {**src, "covered": str(actual <= row.first_p90).lower()})
        ins.eta_abs_error.record(abs(row.first_p50 - actual), src)
        # two monotonic counters (not an up/down gauge): rate()/increase() survive replica restarts
        ins.eta_early.add(max(0.0, actual - row.first_p50), src)
        ins.eta_late.add(max(0.0, row.first_p50 - actual), src)

    async def _create(self, s: AsyncSession, env: Envelope) -> None:
        p, store = env.payload, env.payload["store_id"]
        slat, slng = STORES[store]                  # unknown store: permanent error -> DLQ
        recent = env.occurred_at - timedelta(minutes=20)  # an order whose delivery event was lost ages out
        active = (await s.execute(select(func.count()).select_from(order_state).where(
            order_state.c.store_id == store, order_state.c.status == "active",
            order_state.c.created_at > recent))).scalar_one()
        in_pick = (await s.execute(select(func.count()).select_from(order_state).where(
            order_state.c.store_id == store, order_state.c.status == "active",
            order_state.c.created_at > recent, order_state.c.pick_done_at.is_(None)))).scalar_one()
        idle_near, est_km = await self._fleet_context(slat, slng)
        await s.execute(pg_insert(order_state).values(
            order_id=env.order_id, store_id=store, cust_lat=p["lat"], cust_lng=p["lng"],
            cust_cell=cell_of(p["lat"], p["lng"]), status="active", stage=0,
            created_at=env.occurred_at, active_orders=active,
            queue_depth=max(0, in_pick - PICKERS_PER_STORE), idle_near=idle_near,
            est_rider_km=est_km).on_conflict_do_nothing())

    def _features(self, row, now: datetime) -> dict:
        slat, slng = STORES[row.store_id]
        t = row.created_at.timestamp()               # order-time hour/day/rain, as in training
        pick_min = None
        if row.pick_started_at and row.pick_done_at:
            pick_min = self.clock.sim_minutes((row.pick_done_at - row.pick_started_at).total_seconds())
        day = self.clock.day(t)
        return dict(
            stage=row.stage, elapsed_min=self.clock.sim_minutes((now - row.created_at).total_seconds()),
            dist_km=haversine_km(slat, slng, row.cust_lat, row.cust_lng),
            rider_to_store_km=(row.rider_km if row.stage >= 2 and row.rider_km else row.est_rider_km),
            idle_riders_near=row.idle_near, active_orders_in_store=row.active_orders,
            picker_queue_depth=row.queue_depth, hour=self.clock.hour(t), dow=day % 7,
            raining=int(is_raining(day)), pick_minutes=pick_min if row.stage >= 2 else None,
            customer_cell=row.cust_cell)

    async def _publish(self, s: AsyncSession, row, now: datetime) -> None:
        feats = self._features(row, now)
        # never raises (heuristic fallback inside). In a thread: model inference is CPU work and must not
        # block the event loop, or consumer heartbeats stall and the group rebalances under load (P6 finding)
        eta = await asyncio.to_thread(self.predictor.predict, feats)
        telemetry.instruments().eta_predictions.add(1, {"source": eta.source})
        vals = dict(last_p50=eta.p50, last_p90=eta.p90, model_version=eta.model_version)
        if row.first_p50 is None:
            vals.update(first_p50=eta.p50, first_p90=eta.p90, first_source=eta.source,
                        first_model_version=eta.model_version)
        await s.execute(update(order_state).where(order_state.c.order_id == row.order_id).values(**vals))
        await enqueue(s, META, self.st.topic(Topic.ETA_EVENTS), make_event(
            "eta.updated", row.order_id, stage=STAGES[row.stage], p50_min=eta.p50, p90_min=eta.p90,
            p50_real_s=self.clock.real_seconds(eta.p50), p90_real_s=self.clock.real_seconds(eta.p90),
            source=eta.source, model_version=eta.model_version, elapsed_min=feats["elapsed_min"]))


async def serve(settings: Settings, stop: asyncio.Event, ready: asyncio.Event | None = None):
    redis = make_redis(settings.redis_url)
    clock = await load_clock(redis, settings.sim_start_hour, settings.sim_speed)
    predictor = EtaPredictor(settings.eta_model_dir, enabled=settings.eta_model_enabled)
    handler = EtaHandler(settings, predictor, RiderIndex(redis), clock)

    async def reload_model(sm, stop):
        await run_loop(stop, lambda: asyncio.to_thread(predictor.reload_if_changed),
                       settings.eta_reload_seconds, "eta-reload")

    try:
        await run_service(
            settings, meta=META, group="eta",
            consumes=[Topic.ORDER_EVENTS, Topic.PICKING_EVENTS, Topic.DISPATCH_EVENTS],
            produces=[Topic.ETA_EVENTS], handler=handler.handle, stop=stop,
            extra=[reload_model], ready=ready)
    finally:
        await redis.aclose()
