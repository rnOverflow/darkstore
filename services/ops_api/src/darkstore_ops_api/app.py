"""Ops API: the aggregates behind the ops dashboard. Contract: docs/api/realtime.md.

READ-ONLY queries across service schemas (orders, picking, eta) and the Redis rider index. All
`/api/ops/*` routes need an ops token; `/api/city` is public (static city layout).
"""
from __future__ import annotations

import logging

import h3
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from darkstore_common.city import CENTER, HALF_SPAN_DEG, STORES
from darkstore_common.geo import HEAT_RES, cell_of
from darkstore_common.riders import RiderIndex
from darkstore_common.settings import Settings
from darkstore_common.tokens import InvalidToken, verify

STATUSES = ["created", "reserved", "picking", "picked", "assigned", "out_for_delivery",
            "delivered", "cancelled", "rejected"]
MAX_CELLS = 400
log = logging.getLogger("darkstore.ops_api")


def create_app(settings: Settings, engine: AsyncEngine, redis: Redis) -> FastAPI:
    app = FastAPI(title="ops-api")
    index = RiderIndex(redis)

    def require_ops(authorization: str | None = Header(default=None)) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "missing bearer token")
        try:
            claims = verify(settings.token_secret, token)
        except InvalidToken:
            raise HTTPException(401, "invalid or expired token") from None
        if claims.role != "ops":
            raise HTTPException(403, "ops role required")

    async def query(sql: str, **params):
        """Run a read-only query; a schema that does not exist yet (service not started) is empty."""
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text(sql), params)).all()
        except ProgrammingError as e:
            if "does not exist" in str(e.orig):       # that service has not created its schema yet
                return []
            log.exception("ops query failed")
            raise HTTPException(500, "query failed") from None

    async def all_riders() -> list[dict]:
        ids = await index.all_ids()
        out = []
        for rid, h in zip(ids, await index.get_many(ids)):
            if h:
                out.append({"id": rid, "lat": float(h["lat"]), "lng": float(h["lng"]),
                            "status": h.get("status", "idle"), "order_id": h.get("order_id") or None})
        return out

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/city")
    async def city():
        lat, lng = CENTER
        return {"center": [lat, lng],
                "bounds": [[lat - HALF_SPAN_DEG, lng - HALF_SPAN_DEG], [lat + HALF_SPAN_DEG, lng + HALF_SPAN_DEG]],
                "stores": {k: [v[0], v[1]] for k, v in STORES.items()}, "sim_speed": settings.sim_speed}

    @app.get("/api/ops/summary", dependencies=[Depends(require_ops)])
    async def summary():
        funnel = {s: 0 for s in STATUSES}
        for status, n in await query("select status, count(*) from orders.orders "
                                     "where created_at > now() - interval '60 minutes' group by 1"):
            funnel[status] = n
        n5 = await query("select count(*) from orders.orders where created_at > now() - interval '5 minutes'")
        pick = await query(
            "select count(*), percentile_cont(0.95) within group "
            "(order by extract(epoch from (completed_at - started_at))) from picking.picks "
            "where status = 'completed' and completed_at > now() - interval '10 minutes'")
        samples, p95_s = (pick[0][0], pick[0][1]) if pick else (0, None)
        riders = {"idle": 0, "assigned": 0, "cooldown": 0}
        for r in await all_riders():
            riders[r["status"]] = riders.get(r["status"], 0) + 1
        return {"funnel": funnel,
                "pick_p95_sim_min": None if p95_s is None else float(p95_s) * settings.sim_speed / 60.0,
                "pick_samples": samples, "orders_last_5m": n5[0][0] if n5 else 0, "riders": riders,
                "sim_speed": settings.sim_speed, "generated_at": datetime.now(timezone.utc).isoformat()}

    @app.get("/api/ops/riders", dependencies=[Depends(require_ops)])
    async def riders():
        return {"riders": await all_riders()}

    @app.get("/api/ops/heatmap", dependencies=[Depends(require_ops)])
    async def heatmap():
        cells: dict[str, dict] = {}
        for r in await all_riders():
            if r["status"] == "idle":
                c = cells.setdefault(cell_of(r["lat"], r["lng"], HEAT_RES), {"supply": 0, "demand": 0})
                c["supply"] += 1
        for lat, lng in await query("select cust_lat, cust_lng from eta.order_state "
                                    "where status = 'active' and created_at > now() - interval '10 minutes'"):
            c = cells.setdefault(cell_of(lat, lng, HEAT_RES), {"supply": 0, "demand": 0})
            c["demand"] += 1
        busiest = sorted(cells.items(), key=lambda kv: -(kv[1]["supply"] + kv[1]["demand"]))[:MAX_CELLS]
        return {"cells": [{"cell": cell, **v, "boundary": [[la, lo] for la, lo in h3.cell_to_boundary(cell)]}
                          for cell, v in busiest]}

    @app.get("/api/ops/late", dependencies=[Depends(require_ops)])
    async def late():
        rows = await query(
            "select s.order_id, o.status, s.store_id, "
            "  extract(epoch from (now() - s.created_at)) * :speed / 60.0 as elapsed, s.first_p90 "
            "from eta.order_state s join orders.orders o using (order_id) "
            "where s.status = 'active' and s.first_p90 is not null "
            "  and o.status not in ('delivered', 'cancelled', 'rejected') "
            "  and extract(epoch from (now() - s.created_at)) * :speed / 60.0 > s.first_p90 "
            "order by extract(epoch from (now() - s.created_at)) * :speed / 60.0 - s.first_p90 desc "
            "limit 20", speed=settings.sim_speed)
        return {"late": [{"order_id": r[0], "status": r[1], "store_id": r[2], "elapsed_sim_min": float(r[3]),
                          "promised_p90_sim_min": float(r[4]), "over_by_sim_min": float(r[3]) - float(r[4])}
                         for r in rows]}

    return app
