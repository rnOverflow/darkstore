from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from darkstore_common.events import OrderCreated as CreateOrder
from darkstore_common.settings import Settings

from . import logic
from .models import orders


def create_app(sm: async_sessionmaker[AsyncSession], st: Settings) -> FastAPI:
    app = FastAPI(title="order-service")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/orders", status_code=201)
    async def create(body: CreateOrder, response: Response,
                     idempotency_key: str | None = Header(
                         default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")):
        try:
            async with sm.begin() as s:
                order_id, created, status = await logic.create_order(
                    s, st, customer_id=body.customer_id, store_id=body.store_id,
                    items=[i.model_dump() for i in body.items], lat=body.lat, lng=body.lng,
                    idempotency_key=idempotency_key)
        except logic.Conflict as e:
            raise HTTPException(409, str(e))
        if not created:
            response.status_code = 200
        return {"order_id": order_id, "status": status}

    @app.get("/orders/{order_id}")
    async def get(order_id: str):
        async with sm() as s:
            row = (await s.execute(select(orders).where(orders.c.order_id == order_id))).first()
        if row is None:
            raise HTTPException(404, "order not found")
        return {"order_id": row.order_id, "status": row.status, "store_id": row.store_id,
                "items": row.items, "cancel_reason": row.cancel_reason}

    @app.post("/orders/{order_id}/cancel")
    async def cancel(order_id: str):
        try:
            async with sm.begin() as s:
                status = await logic.cancel_order(s, st, order_id, "customer")
        except logic.NotFound:
            raise HTTPException(404, "order not found")
        except logic.Conflict as e:
            raise HTTPException(409, str(e))
        return {"order_id": order_id, "status": status}

    return app
