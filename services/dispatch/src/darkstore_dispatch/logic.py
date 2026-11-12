from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from darkstore_common.city import STORES
from darkstore_common.envelope import Envelope
from darkstore_common.events import make_event
from darkstore_common.outbox import enqueue
from darkstore_common.riders import RiderIndex
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from .models import META, assignment_log, assignments


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _log(s: AsyncSession, order_id: str, rider_id: str, action: str) -> None:
    await s.execute(assignment_log.insert().values(order_id=order_id, rider_id=rider_id, action=action))


async def _row(s: AsyncSession, order_id: str, lock: bool = True):
    q = select(assignments).where(assignments.c.order_id == order_id)
    return (await s.execute(q.with_for_update() if lock else q)).first()


async def handle(s: AsyncSession, index: RiderIndex, env: Envelope) -> None:
    oid, et = env.order_id, env.event_type
    if et == "order.created":
        store = env.payload["store_id"]
        lat, lng = STORES[store]  # KeyError -> permanent -> DLQ: unknown store is not retryable
        await s.execute(pg_insert(assignments).values(
            order_id=oid, store_id=store, store_lat=lat, store_lng=lng,
            dest_lat=env.payload["lat"], dest_lng=env.payload["lng"], status="awaiting_pick")
            .on_conflict_do_nothing())  # a cancel tombstone may already be here: it wins
    elif et == "picking.completed":
        row = await _row(s, oid)
        if row is None:
            raise LookupError(f"order.created for {oid} not seen yet")  # cross-topic: retry
        if row.status == "awaiting_pick":
            await s.execute(update(assignments).where(assignments.c.order_id == oid)
                            .values(status="pending", ready_at=_now()))
    elif et == "order.cancelled":
        row = await _row(s, oid)
        if row is None:  # cancel overtook order.created: tombstone so created/picking become no-ops
            await s.execute(pg_insert(assignments).values(order_id=oid, status="cancelled")
                            .on_conflict_do_nothing())
            return
        held = await index.order_rider(oid)  # Redis is the truth about who holds the order
        if held:
            if row.status == "assigned" and row.rider_id == held:
                await _log(s, oid, held, "cancel_release")  # log first: see ordering note below
            # else: an orphaned claim (match tx rolled back) that never reached the log
            await index.release(held, oid)  # rider goes straight back to the idle pool
        if row.status not in ("delivered", "cancelled", "picked_up"):
            await s.execute(update(assignments).where(assignments.c.order_id == oid)
                            .values(status="cancelled", rider_id=None))
    elif et in ("rider.picked_up", "rider.delivered"):
        row = await _row(s, oid)
        rider = env.payload["rider_id"]
        if row is None or row.rider_id != rider:
            return  # stale: from a rider we already reassigned away from
        if et == "rider.picked_up":
            if row.status != "assigned":
                return  # duplicate / cancelled
            await s.execute(update(assignments).where(assignments.c.order_id == oid)
                            .values(status="picked_up"))
            await _log(s, oid, rider, "picked_up")
        else:
            if row.status == "assigned":  # delivered overtook picked_up (retry path): wait for it
                raise LookupError("rider.picked_up not applied yet")
            if row.status != "picked_up":
                return  # duplicate
            await s.execute(update(assignments).where(assignments.c.order_id == oid)
                            .values(status="delivered"))
            # ORDERING MATTERS: the log row is inserted before the rider is released. Any later
            # 'assigned' row for this rider can only be written after the release, so it always
            # gets a higher id than this 'delivered' row and the log replays as non-overlapping.
            await _log(s, oid, rider, "delivered")
            await index.release(rider, oid)


async def match_pending(sm: async_sessionmaker[AsyncSession], index: RiderIndex, st: Settings,
                        limit: int = 100, max_misses: int = 5) -> int:
    """Assign riders to ready orders, oldest first.

    One DB transaction PER ORDER: the Redis claim and its DB row commit together, so a failure
    on order k can never strand claims made for orders 1..k-1. Safe with several replicas
    (SKIP LOCKED) and after crashes (the Redis claim is keyed by order and re-used on retry).
    After `max_misses` consecutive orders find nobody free the fleet is exhausted: stop early
    instead of burning Redis calls (it is retried on the next loop tick).
    """
    async with sm() as s:
        ids = (await s.execute(select(assignments.c.order_id).where(assignments.c.status == "pending")
                               .order_by(assignments.c.ready_at).limit(limit))).scalars().all()
    out, n, misses = st.topic(Topic.DISPATCH_EVENTS), 0, 0
    for oid in ids:
        async with sm.begin() as s:
            r = (await s.execute(select(assignments).where(
                assignments.c.order_id == oid, assignments.c.status == "pending")
                .with_for_update(skip_locked=True))).first()
            if r is None:
                continue  # taken by another replica or cancelled meanwhile
            res = await index.match(r.order_id, r.store_lat, r.store_lng, r.dest_lat, r.dest_lng)
            if res is None:
                misses += 1
                if misses >= max_misses:
                    break
                continue
            misses = 0
            rider_id, eta_min, rider_km = res
            now = _now()
            await s.execute(update(assignments).where(assignments.c.order_id == r.order_id).values(
                status="assigned", rider_id=rider_id, assigned_at=now, attempts=r.attempts + 1,
                pickup_deadline=now + timedelta(seconds=st.pickup_timeout_seconds)))
            await _log(s, r.order_id, rider_id, "assigned")
            await enqueue(s, META, out, make_event(
                "rider.assigned", r.order_id, rider_id=rider_id, store_lat=r.store_lat,
                store_lng=r.store_lng, dest_lat=r.dest_lat, dest_lng=r.dest_lng,
                eta_to_store_s=eta_min * 60, rider_to_store_km=rider_km))
            n += 1
    return n


IN_FLIGHT = ("at_store", "to_customer", "at_customer", "done")


async def reassign_timed_out(s: AsyncSession, index: RiderIndex, st: Settings) -> int:
    """Rider did not pick up in time: bench them for a cooldown and put the order back in the queue
    (keeping its original queue position). A rider who already has the order is left alone."""
    rows = (await s.execute(
        select(assignments).where(assignments.c.status == "assigned",
                                  assignments.c.pickup_deadline < _now())
        .with_for_update(skip_locked=True))).all()
    n = 0
    for r in rows:
        h = await index.get(r.rider_id)
        if h.get("order_id") == r.order_id and h.get("phase") in IN_FLIGHT:
            # picked up already; the rider.picked_up event is just lagging. Give it more time.
            await s.execute(update(assignments).where(assignments.c.order_id == r.order_id).values(
                pickup_deadline=_now() + timedelta(seconds=st.pickup_timeout_seconds)))
            continue
        await _log(s, r.order_id, r.rider_id, "timed_out")  # log first, then release
        await index.release(r.rider_id, r.order_id, cooldown_s=st.rider_cooldown_seconds)
        await s.execute(update(assignments).where(assignments.c.order_id == r.order_id)
                        .values(status="pending", rider_id=None))  # ready_at unchanged: keeps its turn
        n += 1
    return n


async def reconcile(s: AsyncSession, index: RiderIndex, grace: float) -> int:
    """Release riders the fleet thinks are busy but dispatch's database does not back.

    Redis and Postgres cannot commit together, so a crash between them can leak a claim.
    Only claims older than `grace` are touched (a fresh claim may still be mid-transaction).
    """
    ids = await index.all_ids()
    now = time.time()
    busy = [(rid, h) for rid, h in zip(ids, await index.get_many(ids))
            if h.get("status") == "assigned" and now - float(h.get("assigned_at", now)) > grace]
    if not busy:
        return 0
    rows = {r.order_id: r for r in (await s.execute(select(assignments).where(
        assignments.c.order_id.in_([h["order_id"] for _, h in busy])))).all()}
    n = 0
    for rid, h in busy:
        r = rows.get(h["order_id"])
        if r is None or r.rider_id != rid or r.status not in ("assigned", "picked_up"):
            n += bool(await index.release(rid, h["order_id"]))
    return n
