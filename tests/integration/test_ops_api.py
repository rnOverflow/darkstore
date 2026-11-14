"""Ops API: auth and the aggregates the ops dashboard shows. Real Postgres/Redis, real services."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from darkstore_common.db import make_engine
from darkstore_common.riders import make_redis
from darkstore_common.tokens import sign
from darkstore_eta.service import serve as serve_eta
from darkstore_ops_api.app import create_app

from .test_dispatch import KM, S0, _eq, fleet, place, until

pytestmark = pytest.mark.integration
SECRET = "ops-test-secret-" + "x" * 20
OPS = {"Authorization": "Bearer " + sign(SECRET, role="ops", ttl_s=600)}


class Ops:
    def __init__(self, c):
        self.engine = make_engine(c.env.database_url)
        self.redis = make_redis(c.env.redis_url)
        st = c.env.model_copy(update={"token_secret": SECRET})
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(st, self.engine, self.redis)),
                                      base_url="http://ops")

    async def close(self):
        await self.http.aclose()
        await self.engine.dispose()
        await self.redis.aclose()


async def test_city_is_public_but_ops_endpoints_need_an_ops_token(env):
    async with fleet(env, seed=[("r1", S0[0], S0[1], False)]) as c:
        o = Ops(c)
        city = (await o.http.get("/api/city")).json()
        assert len(city["stores"]) == 6 and city["sim_speed"] == c.env.sim_speed and len(city["center"]) == 2
        for path in ("summary", "riders", "heatmap", "late"):
            assert (await o.http.get(f"/api/ops/{path}")).status_code == 401
            assert (await o.http.get(f"/api/ops/{path}", headers={"Authorization": "Bearer nope"})).status_code == 401
            cust = {"Authorization": "Bearer " + sign(SECRET, role="customer", order_id="o", ttl_s=60)}
            assert (await o.http.get(f"/api/ops/{path}", headers=cust)).status_code == 403
        await o.close()


async def test_summary_funnel_pick_p95_riders_and_heatmap_reflect_real_activity(env):
    async with fleet(env, seed=[("r1", S0[0] + 0.3 * KM, S0[1], False), ("r2", S0[0] + 0.6 * KM, S0[1], False),
                                ("r3", S0[0] + 5 * KM, S0[1], False)], pick_seconds=1.0) as c:
        o = Ops(c)
        oids = [await place(c, north_km=0.5 + i * 0.2) for i in range(4)]
        for oid in oids:
            await until(lambda oid=oid: _eq(c.status(oid), "delivered"), timeout=90)
        s = (await o.http.get("/api/ops/summary", headers=OPS)).json()
        assert s["funnel"]["delivered"] == 4 and sum(s["funnel"].values()) == 4
        assert set(s["funnel"]) >= {"created", "reserved", "picking", "picked", "assigned",
                                    "out_for_delivery", "delivered", "cancelled", "rejected"}
        assert s["pick_samples"] == 4 and s["pick_p95_sim_min"] is not None
        # 1 s real pick at 600x = 10 sim minutes
        assert 8 < s["pick_p95_sim_min"] < 14
        assert s["orders_last_5m"] == 4 and s["sim_speed"] == c.env.sim_speed
        assert sum(s["riders"].values()) == 3
        riders = (await o.http.get("/api/ops/riders", headers=OPS)).json()["riders"]
        assert {r["id"] for r in riders} == {"r1", "r2", "r3"}
        assert all(set(r) >= {"id", "lat", "lng", "status", "order_id"} for r in riders)
        hm = (await o.http.get("/api/ops/heatmap", headers=OPS)).json()["cells"]
        assert hm and all(len(cell["boundary"]) in (5, 6) for cell in hm)       # hexagons (pentagons rare)
        assert sum(cell["supply"] for cell in hm) >= 1
        await o.close()


async def test_late_orders_are_those_past_their_promised_p90_worst_first_and_capped(env):
    async with fleet(env, seed=[("r1", S0[0], S0[1], False)], extra=(serve_eta,), eta_model_enabled=False,
                     sim_speed=600.0) as c:
        o = Ops(c)
        now = datetime.now(timezone.utc)
        async with c.engine.begin() as conn:
            for i, (age_min_real, p90) in enumerate([(5, 10.0), (5, 100000.0), (6, 10.0), (1, 10.0)]):
                oid = f"late-{i}"
                await conn.execute(text("insert into orders.orders (order_id, customer_id, store_id, items, status) "
                                        "values (:o,'c','store-0','[]','assigned')"), {"o": oid})
                await conn.execute(text(
                    "insert into eta.order_state (order_id, store_id, cust_lat, cust_lng, cust_cell, status, stage,"
                    " created_at, first_p50, first_p90) values (:o,'store-0',12.9,77.6,'c','active',2,:t,5,:p)"),
                    {"o": oid, "t": now - timedelta(minutes=age_min_real), "p": p90})
        late = (await o.http.get("/api/ops/late", headers=OPS)).json()["late"]
        ids = [x["order_id"] for x in late]
        assert "late-1" not in ids and set(ids) == {"late-0", "late-2", "late-3"}    # p90 100000 is not late
        assert ids[0] == "late-2"                                                     # 6 min old: over by the most
        top = late[0]
        assert top["status"] == "assigned" and top["store_id"] == "store-0"
        assert top["over_by_sim_min"] == pytest.approx(top["elapsed_sim_min"] - top["promised_p90_sim_min"], abs=1e-6)
        await o.close()


async def test_heatmap_demand_counts_only_orders_still_in_flight(env):
    async with fleet(env, seed=[("r1", S0[0], S0[1], False)], extra=(serve_eta,), eta_model_enabled=False) as c:
        o = Ops(c)
        now = datetime.now(timezone.utc)

        async def eta_schema_ready():   # the eta service creates its tables asynchronously at startup
            return (await c.q("select to_regclass('eta.order_state') is not null"))[0][0]
        await until(eta_schema_ready, timeout=30)
        async with c.engine.begin() as conn:
            for oid, status in (("d-active", "active"), ("d-done", "delivered"), ("d-cancelled", "cancelled")):
                await conn.execute(text(
                    "insert into eta.order_state (order_id, store_id, cust_lat, cust_lng, cust_cell, status, stage,"
                    " created_at) values (:o,'store-0',12.9,77.6,'c',:s,2,:t)"), {"o": oid, "s": status, "t": now})
        cells = (await o.http.get("/api/ops/heatmap", headers=OPS)).json()["cells"]
        assert sum(cell["demand"] for cell in cells) == 1
        await o.close()
