from __future__ import annotations

from collections import Counter

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from darkstore_common.envelope import Envelope
from darkstore_common.events import make_event
from darkstore_common.outbox import enqueue
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from .models import META, reservations, stock


async def reserve(s: AsyncSession, st: Settings, env: Envelope) -> None:
    oid, store = env.order_id, env.payload["store_id"]
    if (await s.execute(select(reservations.c.order_id).where(reservations.c.order_id == oid))).first():
        return  # same order delivered again under a different event_id
    want = Counter()
    for it in env.payload["items"]:
        want[it["sku"]] += it["qty"]
    # lock rows in sku order: concurrent orders cannot deadlock
    rows = (await s.execute(select(stock).where(stock.c.store_id == store, stock.c.sku.in_(want))
                            .order_by(stock.c.sku).with_for_update())).all()
    have = {r.sku: r.available for r in rows}
    bad = next((k for k, q in sorted(want.items()) if have.get(k, 0) < q), None)
    topic = st.topic(Topic.INVENTORY_EVENTS)
    items = [{"sku": k, "qty": q} for k, q in sorted(want.items())]
    if bad:
        await s.execute(reservations.insert().values(order_id=oid, store_id=store, items=items,
                                                     status="rejected"))
        await enqueue(s, META, topic, make_event("inventory.rejected", oid,
                                                 reason="insufficient_stock", sku=bad))
        return
    for sku, q in want.items():
        await s.execute(update(stock).where(stock.c.store_id == store, stock.c.sku == sku)
                        .values(available=stock.c.available - q))
    await s.execute(reservations.insert().values(order_id=oid, store_id=store, items=items,
                                                 status="reserved"))
    await enqueue(s, META, topic, make_event("inventory.reserved", oid, store_id=store, items=items))


async def release(s: AsyncSession, env: Envelope) -> None:
    """Compensation for order.cancelled. Idempotent: only a 'reserved' row is released."""
    row = (await s.execute(select(reservations).where(reservations.c.order_id == env.order_id)
                           .with_for_update())).first()
    if row is None:
        # cancel overtook order.created (e.g. created is sitting on a retry topic). Leave a tombstone
        # so reserve() sees an existing row and never takes stock for a cancelled order.
        await s.execute(pg_insert(reservations).values(
            order_id=env.order_id, items=[], status="released").on_conflict_do_nothing())
        return
    if row.status != "reserved":
        return
    for it in row.items:
        await s.execute(update(stock).where(stock.c.store_id == row.store_id, stock.c.sku == it["sku"])
                        .values(available=stock.c.available + it["qty"]))
    await s.execute(update(reservations).where(reservations.c.order_id == env.order_id)
                    .values(status="released"))


async def seed_default(s: AsyncSession, stores: int = 6, skus: int = 20, qty: int = 1000) -> None:
    if (await s.execute(select(stock.c.sku).limit(1))).first():
        return
    await s.execute(stock.insert(), [{"store_id": f"store-{i}", "sku": f"sku-{j}", "available": qty}
                                     for i in range(stores) for j in range(skus)])
