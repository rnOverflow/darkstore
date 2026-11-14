from __future__ import annotations

import secrets

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from darkstore_common.city import deliverable
from darkstore_common.events import ID, OrderCreated
from darkstore_common.settings import Settings
from darkstore_common.tokens import Claims, InvalidToken, sign, verify

from . import logic
from .models import orders


class NewOrder(OrderCreated):
    """What a client may send. `customer_id` is NOT accepted: identity comes from the session token."""
    model_config = ConfigDict(extra="ignore")
    customer_id: str | None = Field(default=None, exclude=True, **ID)


def create_app(sm: async_sessionmaker[AsyncSession], st: Settings) -> FastAPI:
    docs = {} if st.expose_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="order-service", **docs)

    def claims_from(authorization: str | None) -> Claims:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        try:
            return verify(st.token_secret, token.strip())
        except InvalidToken:
            raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})

    def order_claims(authorization: str | None, order_id: str) -> Claims:
        """Per-order endpoints: the order's own tracking token (or ops). Checked BEFORE the lookup, so an
        unauthorised caller cannot tell whether an id exists."""
        c = claims_from(authorization)
        if c.role == "ops" or (c.role == "customer" and c.order_id == order_id):
            return c
        raise HTTPException(403, "token is not valid for this order")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/sessions", status_code=201)
    async def session():
        """Anonymous guest session: a server-assigned customer id + a token that can only create orders.
        (Real customer accounts are out of scope; see ADR-0007.)"""
        cid = "g-" + secrets.token_urlsafe(9)
        return {"customer_id": cid, "token": sign(st.token_secret, role="guest", customer_id=cid,
                                                  ttl_s=st.guest_token_ttl)}

    @app.post("/orders", status_code=201)
    async def create(body: NewOrder, response: Response,
                     authorization: str | None = Header(default=None),
                     idempotency_key: str | None = Header(
                         default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")):
        c = claims_from(authorization)
        if c.role != "guest" or not c.customer_id:
            raise HTTPException(403, "a guest session token is required to place orders")
        if not deliverable(body.store_id, body.lat, body.lng):
            raise HTTPException(422, "destination is outside the delivery area of this store")
        try:
            async with sm.begin() as s:
                order_id, created, status = await logic.create_order(
                    s, st, customer_id=c.customer_id, store_id=body.store_id,
                    items=[i.model_dump() for i in body.items], lat=body.lat, lng=body.lng,
                    idempotency_key=idempotency_key)
        except logic.Conflict as e:
            raise HTTPException(409, str(e))
        if not created:
            response.status_code = 200
        # Also on a replay: idempotency keys are scoped to the session's customer_id, so a replay is by
        # the very session that created the order. Without this a lost 201 would leave the customer
        # with an order they can neither track nor cancel.
        return {"order_id": order_id, "status": status,
                "tracking_token": sign(st.token_secret, role="customer", order_id=order_id,
                                       ttl_s=st.tracking_token_ttl)}

    @app.get("/orders/{order_id}")
    async def get(order_id: str, authorization: str | None = Header(default=None)):
        order_claims(authorization, order_id)
        async with sm() as s:
            row = (await s.execute(select(orders).where(orders.c.order_id == order_id))).first()
        if row is None:
            raise HTTPException(404, "order not found")
        return {"order_id": row.order_id, "status": row.status, "store_id": row.store_id,
                "items": row.items, "cancel_reason": row.cancel_reason}

    @app.post("/orders/{order_id}/cancel")
    async def cancel(order_id: str, authorization: str | None = Header(default=None)):
        order_claims(authorization, order_id)
        try:
            async with sm.begin() as s:
                status = await logic.cancel_order(s, st, order_id, "customer")
        except logic.NotFound:
            raise HTTPException(404, "order not found")
        except logic.Conflict as e:
            raise HTTPException(409, str(e))
        return {"order_id": order_id, "status": status}

    return app
