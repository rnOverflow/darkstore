"""Dispatch + rider-sim: the full order lifecycle to delivery, reassignment, cancellation."""
from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest

from darkstore_common.city import STORES
from darkstore_common.riders import RiderIndex, make_redis
from darkstore_dispatch.service import serve as serve_dispatch
from darkstore_inventory.service import serve as serve_inventory
from darkstore_order.service import serve as serve_order
from darkstore_picking.service import serve as serve_picking
from darkstore_rider_sim.service import serve as serve_rider_sim

from .helpers import running, until
from .test_saga import Ctx, _eq, _ok, _port

pytestmark = pytest.mark.integration
S0 = STORES["store-0"]
KM = 1 / 111.195


def assert_no_double_assignment(log):
    """Replay the append-only assignment log: a rider never holds two orders, an order never two riders."""
    by_rider, by_order = {}, {}
    for oid, rid, action in log:
        if action == "assigned":
            assert rid not in by_rider, f"{rid} double-assigned: {by_rider[rid]} and {oid}"
            assert oid not in by_order, f"{oid} double-assigned: {by_order[oid]} and {rid}"
            by_rider[rid], by_order[oid] = oid, rid
        elif action in ("timed_out", "cancel_release", "delivered"):
            assert by_rider.get(rid) == oid, f"{action}: {rid} did not hold {oid}"
            del by_rider[rid], by_order[oid]
        else:  # picked_up
            assert by_rider.get(rid) == oid
    return by_rider


@asynccontextmanager
async def fleet(env, seed=(), dispatchers=1, **overrides):
    """order + inventory + picking + dispatch + rider-sim. seed: (id, lat, lng, flaky) tuples; otherwise rider-sim seeds `riders` of them."""
    port = _port()
    base = dict(http_port=port, sim_speed=600.0, sim_tick=0.1, pickup_timeout_seconds=20.0,
                rider_cooldown_seconds=3.0, match_interval=0.1, seed_stock=True,
                riders=0 if seed else 100)
    st = env.model_copy(update={**base, **overrides})
    ctx = Ctx(st, port)
    rds = make_redis(st.redis_url)
    ctx.index = RiderIndex(rds)
    for rid, lat, lng, flaky in seed:
        await ctx.index.add_rider(rid, lat, lng, flaky=flaky)
    if seed:
        await rds.set("ds:seeded", "1")
    ctx.rds = rds
    async with AsyncExitStack() as x:
        svcs = (serve_order, serve_inventory, serve_picking, serve_rider_sim,
                *[serve_dispatch] * dispatchers)  # >1 dispatcher: matchers genuinely race for riders
        for svc in svcs:
            await x.enter_async_context(running(lambda stop, svc=svc: svc(st, stop)))
        await until(lambda: _ok(ctx), timeout=60)
        try:
            yield ctx
        finally:
            await ctx.http.aclose()
            await ctx.engine.dispose()
            await rds.aclose()


async def place(c, store="store-0", north_km=1.0, qty=1, customer="c1"):
    slat, slng = STORES[store]
    r = await c.http.post("/orders", json={
        "customer_id": customer, "store_id": store, "lat": slat + north_km * KM, "lng": slng,
        "items": [{"sku": "sku-1", "qty": qty}]})
    assert r.status_code == 201, r.text
    return r.json()["order_id"]


async def dlog(c):
    return [tuple(r) for r in await c.q(
        "select order_id, rider_id, action from dispatch.assignment_log order by id")]


async def test_order_flows_to_delivered_and_rider_returns_to_the_pool(env):
    async with fleet(env, seed=[("r1", S0[0] + 0.5 * KM, S0[1], False)]) as c:
        oid = await place(c)
        await until(lambda: _eq(c.status(oid), "delivered"), timeout=60)
        assert [a for _, _, a in await dlog(c)] == ["assigned", "picked_up", "delivered"]
        evs = await c.q("""select body::json->>'event_type' from (
            select body, id, created_at from orders.outbox union all
            select body, id, created_at from dispatch.outbox union all
            select body, id, created_at from rider_sim.outbox) t
            where body::json->>'order_id'=:o order by created_at, id""", o=oid)
        assert [e[0] for e in evs if e[0].startswith(("rider", "order."))] == [
            "order.created", "rider.assigned", "rider.picked_up", "order.out_for_delivery",
            "rider.delivered", "order.delivered"]
        await until(lambda: _eq(_idle(c), 1))  # rider is searchable again, where they delivered
        assert await c.index.order_rider(oid) is None


async def _idle(c):
    return await c.index.idle_count()


async def test_best_rider_by_eta_is_chosen(env):
    async with fleet(env, seed=[("far", S0[0] + 3 * KM, S0[1], False),
                                  ("near", S0[0] + 0.3 * KM, S0[1], False)]) as c:
        oid = await place(c)
        await until(lambda: _eq(c.status(oid), "delivered"), timeout=60)
        assert [(r, a) for _, r, a in await dlog(c)][0] == ("near", "assigned")


async def test_cancel_while_assigned_releases_the_rider(env):
    # crawl speed: the rider will not reach the store before we cancel
    async with fleet(env, seed=[("r1", S0[0] + 2 * KM, S0[1], False)], sim_speed=0.01) as c:
        oid = await place(c)
        await until(lambda: _eq(_dstatus(c, oid), "assigned"), timeout=40)
        assert (await c.http.post(f"/orders/{oid}/cancel")).status_code == 200
        await until(lambda: _eq(_idle(c), 1), timeout=30)
        assert await c.status(oid) == "cancelled"
        assert [a for _, _, a in await dlog(c)] == ["assigned", "cancel_release"]
        assert (await c.index.get("r1"))["status"] == "idle"


async def _dstatus(c, oid):
    rows = await c.q("select status from dispatch.assignments where order_id=:o", o=oid)
    return rows[0][0] if rows else None


async def test_no_show_rider_is_benched_and_the_order_reassigned(env):
    async with fleet(env, seed=[("flaky", S0[0] + 0.2 * KM, S0[1], True),
                                  ("steady", S0[0] + 2.0 * KM, S0[1], False)],
                     pickup_timeout_seconds=3.0, rider_cooldown_seconds=60) as c:
        oid = await place(c)
        await until(lambda: _eq(c.status(oid), "delivered"), timeout=60)
        log = await dlog(c)
        assert [(r, a) for _, r, a in log] == [
            ("flaky", "assigned"), ("flaky", "timed_out"), ("steady", "assigned"),
            ("steady", "picked_up"), ("steady", "delivered")]
        assert (await c.index.get("flaky"))["status"] == "cooldown"  # benched, not re-offered
        assert_no_double_assignment(log)


async def test_orders_queue_when_riders_are_scarce_and_are_served_in_turn(env):
    async with fleet(env, seed=[("only", S0[0] + 0.3 * KM, S0[1], False)]) as c:
        oids = [await place(c, north_km=0.5 + i * 0.1) for i in range(3)]
        for oid in oids:
            await until(lambda oid=oid: _eq(c.status(oid), "delivered"), timeout=90)
        log = await dlog(c)
        assert assert_no_double_assignment(log) == {}
        assert sum(1 for _, r, a in log if a == "delivered" and r == "only") == 3


@pytest.mark.timeout(300)
async def test_gate_500_concurrent_orders_zero_double_assigned_riders(env):
    """P2 gate: 500 orders fired concurrently through the whole pipeline.

    3 dispatch replicas race for 120 riders (seeded by rider-sim); 15% of riders no-show their first assignment (forcing
    reassignments). Proof of "zero double assignment" is checked three independent ways:
    the append-only assignment log, the Redis rider state, and the order rows.
    """
    import asyncio
    import random

    from darkstore_simulator.service import new_order

    n, rng = 500, random.Random(99)
    async with fleet(
            env, dispatchers=3, **{"riders": 120, "flaky_rate": 0.15, "pickup_timeout_seconds": 8.0,
                    "rider_cooldown_seconds": 4.0, "pick_seconds": 0.2}) as c:
        sem = asyncio.Semaphore(100)

        async def fire():
            async with sem:
                r = await c.http.post("/orders", json=new_order(rng))
                assert r.status_code == 201, r.text
                return r.json()["order_id"]

        t0 = time.time()
        oids = await asyncio.gather(*[fire() for _ in range(n)])
        t_placed = time.time() - t0
        assert len(set(oids)) == n

        async def all_delivered():
            rows = await c.q("select count(*) from orders.orders where status='delivered'")
            return rows[0][0] == n
        try:
            await until(all_delivered, timeout=200, interval=1.0)
        except AssertionError:
            diag = {
                "orders": await c.q("select status, count(*) from orders.orders group by 1"),
                "dispatch": await c.q("select status, count(*) from dispatch.assignments group by 1"),
                "riders": [h.get("status") for h in await c.index.get_many(await c.index.all_ids())],
                "stuck_assigned_orders_without_rider": await c.q(
                    "select count(*) from orders.orders where status='assigned' and rider_id is null"),
                "dlq_hint": "check kafka DLQ topics",
            }
            from collections import Counter
            diag["riders"] = dict(Counter(diag["riders"]))
            raise AssertionError(f"gate stalled: {diag}") from None
        t_done = time.time() - t0

        # 1) the audit log: replay it, no rider/order is ever held twice, nothing left dangling
        log = await dlog(c)
        assert assert_no_double_assignment(log) == {}
        actions = [a for _, _, a in log]
        assert actions.count("delivered") == n and actions.count("picked_up") == n
        assert actions.count("timed_out") >= 1, "no-show riders should have forced reassignments"
        assert actions.count("assigned") == n + actions.count("timed_out")
        # 2) every order was delivered by exactly one rider
        per_order = {}
        for oid, rid, a in log:
            if a == "delivered":
                assert oid not in per_order
                per_order[oid] = rid
        assert set(per_order) == set(oids)
        # 3) Redis ends clean: no rider holds an order, no order holds a rider
        await until(lambda: _eq(_busy(c), 0), timeout=60)
        assert await c.rds.keys("ds:order_rider:*") == []
        print(f"\nGATE: {n} orders placed in {t_placed:.1f}s, all delivered in {t_done:.1f}s "
              f"({n / t_done:.1f} orders/s); {actions.count('timed_out')} reassignments; "
              f"0 double assignments across {len(log)} log rows")


async def _busy(c):
    ids = await c.index.all_ids()
    return sum(1 for h in await c.index.get_many(ids) if h.get("status") == "assigned")


# ---------- review findings: crash windows and forged events ----------

async def test_orphaned_claim_is_reconciled(env):
    """A Redis claim whose DB transaction never committed must not strand the rider forever."""
    async with fleet(env, seed=[("r1", S0[0], S0[1], False)], reconcile_grace_seconds=2.0) as c:
        assert await c.index.claim("r1", "ghost-order", store_lat=S0[0], store_lng=S0[1],
                                   dest_lat=S0[0], dest_lng=S0[1], now=time.time() - 60)
        assert (await c.index.get("r1"))["status"] == "assigned"
        await until(lambda: _eq(_idle(c), 1), timeout=30)
        assert await c.index.order_rider("ghost-order") is None


async def test_cancel_releases_whoever_holds_the_order_even_if_dispatch_row_is_pending(env):
    async with fleet(env, seed=[("r1", S0[0], S0[1], False)], sim_speed=0.01,
                     reconcile_grace_seconds=3600) as c:  # reconciler off: cancel must do it
        oid = await place(c)
        await until(lambda: _eq(_dstatus(c, oid), "assigned"), timeout=40)
        # simulate the rolled-back match tx: dispatch row back to pending, Redis claim intact
        await c.q_exec("update dispatch.assignments set status='pending', rider_id=null "
                       "where order_id=:o", o=oid)
        assert (await c.index.get("r1"))["status"] == "assigned"
        assert (await c.http.post(f"/orders/{oid}/cancel")).status_code == 200
        await until(lambda: _eq(_idle(c), 1), timeout=30)
        assert await c.index.order_rider(oid) is None


async def test_rider_stuck_in_at_store_phase_recovers(env):
    """Crash between phase change and event emit: phase 'at_store' must re-emit, not strand."""
    async with fleet(env, seed=[("r1", S0[0] + 0.3 * KM, S0[1], False)], sim_speed=0.01) as c:
        oid = await place(c)
        await until(lambda: _eq(_dstatus(c, oid), "assigned"), timeout=40)
        await c.rds.hset("ds:rider:r1", "phase", "at_store")
        await until(lambda: _eq(c.status(oid), "out_for_delivery"), timeout=30)
        assert (await c.index.get("r1"))["phase"] == "to_customer"


async def test_forged_rider_events_cannot_complete_an_order(env):
    from darkstore_common.events import make_event
    from darkstore_common.topics import Topic

    from .helpers import publish
    async with fleet(env, seed=[("r1", S0[0], S0[1], False)], sim_speed=0.01) as c:
        oid = await place(c)
        await until(lambda: _eq(_dstatus(c, oid), "assigned"), timeout=40)
        topic = env.topic(Topic.DISPATCH_EVENTS)
        await publish(env.kafka_bootstrap, topic, make_event("rider.picked_up", oid, rider_id="evil"))
        await publish(env.kafka_bootstrap, topic, make_event("rider.delivered", oid, rider_id="evil"))
        await publish(env.kafka_bootstrap, topic, make_event("rider.picked_up", oid, rider_id="r1"))
        await until(lambda: _eq(c.status(oid), "out_for_delivery"), timeout=30)  # real rider ok
        await publish(env.kafka_bootstrap, topic, make_event("rider.delivered", oid, rider_id="evil"))
        await asyncio.sleep(3)
        assert await c.status(oid) == "out_for_delivery"  # forged delivery ignored


async def test_event_on_the_wrong_topic_is_rejected_not_obeyed(env):
    from darkstore_common.events import make_event
    from darkstore_common.topics import Topic, dlq_topic

    from .helpers import publish, read_all
    async with fleet(env, seed=[("r1", S0[0], S0[1], False)], sim_speed=0.01) as c:
        oid = await place(c)
        await until(lambda: _eq(_dstatus(c, oid), "assigned"), timeout=40)
        wrong = env.topic(Topic.DISPATCH_EVENTS)  # order.cancelled belongs on order.events
        await publish(env.kafka_bootstrap, wrong, make_event("order.cancelled", oid, reason="forged"))
        msgs = await read_all(env.kafka_bootstrap, dlq_topic(wrong, "dispatch"), wait=20)
        assert len(msgs) == 1
        assert await c.status(oid) != "cancelled" and await _dstatus(c, oid) == "assigned"
