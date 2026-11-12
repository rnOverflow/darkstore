"""Order domain operations. All run inside the caller's transaction."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from darkstore_common.envelope import Envelope
from darkstore_common.events import make_event
from darkstore_common.outbox import enqueue
from darkstore_common.settings import Settings
from darkstore_common.topics import Topic

from .models import META, orders
from .state import OutOfOrder, is_terminal, next_status


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


async def create_order(s: AsyncSession, st: Settings, *, customer_id: str, store_id: str,
                       items: list[dict], lat: float, lng: float,
                       idempotency_key: str | None) -> tuple[str, bool, str]:
    """Returns (order_id, created, status). Same (customer, key) -> same order, no second event."""
    order_id = str(uuid.uuid4())
    r = await s.execute(pg_insert(orders).values(
        order_id=order_id, customer_id=customer_id, store_id=store_id, items=items,
        status="created", idempotency_key=idempotency_key)
        .on_conflict_do_nothing(index_elements=["customer_id", "idempotency_key"])
        .returning(orders.c.order_id))
    if r.first() is None:
        row = (await s.execute(select(orders).where(orders.c.customer_id == customer_id,
                                                    orders.c.idempotency_key == idempotency_key))).one()
        if row.store_id != store_id or row.items != items:
            raise Conflict("Idempotency-Key was already used with a different request")
        return row.order_id, False, row.status
    ev = make_event("order.created", order_id, customer_id=customer_id, store_id=store_id,
                    items=items, lat=lat, lng=lng)
    await enqueue(s, META, st.topic(Topic.ORDER_EVENTS), ev)
    return order_id, True, "created"


async def _lock(s: AsyncSession, order_id: str):
    row = (await s.execute(select(orders).where(orders.c.order_id == order_id)
                           .with_for_update())).first()
    if row is None:
        raise NotFound(order_id)
    return row


async def apply_event(s: AsyncSession, st: Settings, env: Envelope) -> None:
    """Consumer side: move the order along. Raises OutOfOrder/NotFound -> retry.

    Order-service owns the customer-facing lifecycle events: a rider pickup makes the order
    ``out_for_delivery`` and a rider delivery makes it ``delivered``; both announce themselves.
    """
    row = await _lock(s, env.order_id)
    et = env.event_type
    if et in ("rider.picked_up", "rider.delivered"):
        if row.rider_id is None:  # rider.assigned (other topic) not applied yet: retry, don't drop
            raise OutOfOrder(f"{et} before rider.assigned for {env.order_id}")
        if env.payload["rider_id"] != row.rider_id:
            return  # not the rider dispatch assigned (forged, or a rider we were reassigned away from)
    if et == "rider.assigned" and row.status == "assigned":  # reassignment after a no-show
        await s.execute(update(orders).where(orders.c.order_id == env.order_id)
                        .values(rider_id=env.payload["rider_id"]))
        return
    new = next_status(row.status, et)
    if not new:
        return
    if et == "rider.assigned":
        await s.execute(update(orders).where(orders.c.order_id == env.order_id)
                        .values(rider_id=env.payload["rider_id"]))
    out = st.topic(Topic.ORDER_EVENTS)
    if env.event_type == "rider.picked_up":
        new = "out_for_delivery"
        await enqueue(s, META, out, make_event("order.out_for_delivery", env.order_id))
    elif env.event_type == "rider.delivered":
        await enqueue(s, META, out, make_event("order.delivered", env.order_id))
    await s.execute(update(orders).where(orders.c.order_id == env.order_id)
                    .values(status=new, updated_at=func.now()))


async def cancel_order(s: AsyncSession, st: Settings, order_id: str, reason: str) -> str:
    row = await _lock(s, order_id)
    if row.status == "cancelled":
        return "cancelled"  # idempotent
    if is_terminal(row.status):
        raise Conflict(f"order is {row.status}")
    if reason == "customer" and row.status in ("picked_up", "out_for_delivery"):
        raise Conflict("order is already with the rider")
    await s.execute(update(orders).where(orders.c.order_id == order_id)
                    .values(status="cancelled", cancel_reason=reason, updated_at=func.now()))
    await enqueue(s, META, st.topic(Topic.ORDER_EVENTS),
                  make_event("order.cancelled", order_id, reason=reason))
    return "cancelled"


async def expire_stuck(s: AsyncSession, st: Settings) -> int:
    """Saga timeout: orders stuck before dispatch are cancelled so inventory gets released."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=st.order_timeout_seconds)
    ids = (await s.execute(
        select(orders.c.order_id)
        .where(orders.c.status.in_(("created", "reserved", "picking", "picked", "assigned")),
               orders.c.updated_at < cutoff)
        .with_for_update(skip_locked=True))).scalars().all()
    for oid in ids:
        await cancel_order(s, st, oid, "timeout")
    return len(ids)
