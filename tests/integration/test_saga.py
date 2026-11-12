"""Order -> inventory -> picking saga end to end against real Redpanda + Postgres."""
from __future__ import annotations

import asyncio
import socket
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from darkstore_common.events import make_event
from darkstore_common.topics import Topic
from darkstore_inventory.service import serve as serve_inventory
from darkstore_order.service import serve as serve_order
from darkstore_picking.service import serve as serve_picking

from .helpers import publish, running, until

pytestmark = pytest.mark.integration
ITEMS = [{"sku": "milk", "qty": 2}, {"sku": "eggs", "qty": 1}]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class Ctx:
    def __init__(self, env, port):
        self.env, self.port = env, port
        self.engine = create_async_engine(env.database_url)
        self.http = httpx.AsyncClient(base_url=f"http://localhost:{port}", timeout=10)

    async def q(self, sql, **p):
        async with self.engine.connect() as c:
            return (await c.execute(text(sql), p)).all()

    async def q_exec(self, sql, **p):
        async with self.engine.begin() as c:
            await c.execute(text(sql), p)

    async def status(self, oid):
        rows = await self.q("select status from orders.orders where order_id=:o", o=oid)
        return rows[0][0] if rows else None

    async def stock(self, sku="milk"):
        rows = await self.q("select available from inventory.stock where store_id='s1' and sku=:k", k=sku)
        return rows[0][0]

    async def place(self, key=None, items=ITEMS):
        r = await self.http.post("/orders", json={"customer_id": "c1", "store_id": "s1", "items": items},
                                 headers={"Idempotency-Key": key} if key else {})
        assert r.status_code in (200, 201), r.text
        return r.json()["order_id"]

    async def seed(self, milk=10, eggs=10):
        # inventory service creates the schema on startup; wait for it
        await until(self._has_stock_table)
        async with self.engine.begin() as c:
            await c.execute(text("insert into inventory.stock values ('s1','milk',:m),('s1','eggs',:e)"),
                            {"m": milk, "e": eggs})

    async def wait_table(self, name):
        async def ok():
            return (await self.q(f"select to_regclass('{name}')"))[0][0] is not None
        await until(ok)

    async def _has_stock_table(self):
        return (await self.q("select to_regclass('inventory.stock')"))[0][0] is not None


@asynccontextmanager
async def stack(env, *, order=True, inventory=True, picking=True, **overrides):
    port = _port()
    st = env.model_copy(update={"http_port": port, **overrides})
    ctx = Ctx(st, port)
    async with AsyncExitStack() as x:
        if order:
            await x.enter_async_context(running(lambda stop: serve_order(st, stop)))
        if inventory:
            await x.enter_async_context(running(lambda stop: serve_inventory(st, stop)))
        if picking:
            await x.enter_async_context(running(lambda stop: serve_picking(st, stop)))
        await until(lambda: _ok(ctx), timeout=40) if order else None
        try:
            yield ctx
        finally:
            await ctx.http.aclose()
            await ctx.engine.dispose()


async def _ok(ctx):
    try:
        return (await ctx.http.get("/healthz")).status_code == 200
    except httpx.HTTPError:
        return False


async def test_happy_path_reaches_picked_and_decrements_stock(env):
    async with stack(env) as c:
        await c.seed()
        oid = await c.place()
        await until(lambda: _eq(c.status(oid), "picked"))
        assert await c.stock("milk") == 8 and await c.stock("eggs") == 9
        # events are emitted in saga order for this order
        evs = await c.q("""select (body::json->>'event_type') from (
            select body, created_at, id from orders.outbox union all
            select body, created_at, id from inventory.outbox union all
            select body, created_at, id from picking.outbox) t
            where body::json->>'order_id' = :o order by created_at, id""", o=oid)
        assert [e[0] for e in evs] == ["order.created", "inventory.reserved", "picking.started",
                                       "picking.completed"]


async def test_insufficient_stock_rejects_and_leaves_stock_untouched(env):
    async with stack(env) as c:
        await c.seed(milk=1)
        oid = await c.place()
        await until(lambda: _eq(c.status(oid), "rejected"))
        assert await c.stock("milk") == 1 and await c.stock("eggs") == 10


async def test_cancel_releases_inventory_and_order_stays_cancelled(env):
    async with stack(env, pick_seconds=30) as c:  # picking will not finish on its own
        await c.seed()
        oid = await c.place()
        await until(lambda: _eq(c.status(oid), "picking"))
        assert await c.stock("milk") == 8
        assert (await c.http.post(f"/orders/{oid}/cancel")).status_code == 200
        await until(lambda: _eq(c.stock("milk"), 10))
        assert await c.stock("eggs") == 10
        assert await c.status(oid) == "cancelled"
        assert (await c.http.post(f"/orders/{oid}/cancel")).status_code == 200  # idempotent


async def test_duplicate_event_delivery_applies_effects_once(env):
    async with stack(env) as c:
        await c.seed()
        oid = await c.place()
        await until(lambda: _eq(c.status(oid), "picked"))
        rows = await c.q("select body from orders.outbox where body::json->>'event_type'='order.created'")
        created = rows[0][0]
        from darkstore_common.envelope import Envelope
        original = Envelope.model_validate_json(created)
        # 1) the very same event again (same event_id)
        await publish(env.kafka_bootstrap, env.topic(Topic.ORDER_EVENTS), original)
        # 2) a relay-style re-emit: same order, brand new event_id
        again = make_event("order.created", oid, **original.payload)
        await publish(env.kafka_bootstrap, env.topic(Topic.ORDER_EVENTS), again)
        await asyncio.sleep(4)  # let consumers see both
        assert await c.stock("milk") == 8 and await c.stock("eggs") == 9  # reserved once
        assert await c.status(oid) == "picked"
        n = await c.q("select count(*) from inventory.reservations where order_id=:o", o=oid)
        assert n[0][0] == 1


async def test_out_of_order_events_converge_via_retry(env):
    async with stack(env, picking=False) as c:  # we play the picking service by hand
        await c.seed()
        oid = await c.place()
        await until(lambda: _eq(c.status(oid), "reserved"))
        topic = env.topic(Topic.PICKING_EVENTS)
        # completed arrives BEFORE started
        await publish(env.kafka_bootstrap, topic, make_event("picking.completed", oid, store_id="s1"))
        await publish(env.kafka_bootstrap, topic, make_event("picking.started", oid, store_id="s1"))
        await until(lambda: _eq(c.status(oid), "picked"), timeout=40)


async def test_saga_timeout_cancels_and_late_inventory_nets_to_zero(env):
    async with stack(env, inventory=False, picking=False, order_timeout_seconds=1) as c:
        oid = await c.place()
        await until(lambda: _eq(c.status(oid), "cancelled"), timeout=20)
        r = await c.http.get(f"/orders/{oid}")
        assert r.json()["cancel_reason"] == "timeout"
        # inventory comes online late and sees created then cancelled, in order
        async with running(lambda stop: serve_inventory(c.env, stop)):
            await c.seed()
            await until(lambda: _eq(_stock_row(c, oid), "released"), timeout=40)
            assert await c.stock("milk") == 10 and await c.stock("eggs") == 10


async def test_create_is_idempotent_by_key(env):
    async with stack(env, inventory=False, picking=False) as c:
        a = await c.place(key="abc")
        b = await c.place(key="abc")
        assert a == b
        n = await c.q("select count(*) from orders.outbox where body::json->>'event_type'='order.created'")
        assert n[0][0] == 1


async def test_unknown_order_is_404_and_bad_payload_is_422(env):
    async with stack(env, inventory=False, picking=False) as c:
        assert (await c.http.get("/orders/nope")).status_code == 404
        r = await c.http.post("/orders", json={"customer_id": "c", "store_id": "s", "items": []})
        assert r.status_code == 422


async def _eq(coro, want):
    got = await coro
    return got == want


async def _stock_row(c, oid):
    rows = await c.q("select status from inventory.reservations where order_id=:o", o=oid)
    return rows[0][0] if rows else None


async def test_cancel_overtaking_created_never_leaks_stock(env):
    """created is delayed (retry) while cancelled is processed first: stock must not be taken."""
    async with stack(env, order=False, picking=False) as c:
        await c.seed()
        top = env.topic(Topic.ORDER_EVENTS)
        created = make_event("order.created", "ghost", customer_id="c1", store_id="s1", items=ITEMS)
        await publish(env.kafka_bootstrap, top, make_event("order.cancelled", "ghost"))
        await publish(env.kafka_bootstrap, top, created)
        await until(lambda: _eq(_stock_row(c, "ghost"), "released"), timeout=30)
        await asyncio.sleep(2)
        assert await c.stock("milk") == 10 and await c.stock("eggs") == 10
        n = await c.q("select count(*) from inventory.outbox where key='ghost'")
        assert n[0][0] == 0  # no inventory.reserved emitted for a cancelled order


async def test_picking_ignores_reserved_that_arrives_after_cancel(env):
    async with stack(env, order=False, inventory=False) as c:
        await c.wait_table("picking.picks")
        await publish(env.kafka_bootstrap, env.topic(Topic.ORDER_EVENTS),
                      make_event("order.cancelled", "ghost"))

        async def tombstoned():  # different topics are not ordered: wait until the cancel is applied
            return (await c.q("select status from picking.picks where order_id='ghost'")) == [("cancelled",)]
        await until(tombstoned, timeout=30)
        await publish(env.kafka_bootstrap, env.topic(Topic.INVENTORY_EVENTS),
                      make_event("inventory.reserved", "ghost", store_id="s1", items=ITEMS))
        await asyncio.sleep(5)
        rows = await c.q("select status from picking.picks where order_id='ghost'")
        assert rows == [("cancelled",)]
        assert (await c.q("select count(*) from picking.outbox where key='ghost'"))[0][0] == 0


async def test_api_rejects_out_of_bounds_input(env):
    async with stack(env, inventory=False, picking=False) as c:
        post = lambda body, h=None: c.http.post("/orders", json=body, headers=h or {})  # noqa: E731
        ok = {"customer_id": "c1", "store_id": "s1", "items": [{"sku": "milk", "qty": 1}]}
        assert (await post({**ok, "items": [{"sku": "milk", "qty": 101}]})).status_code == 422
        assert (await post({**ok, "items": [{"sku": "x" * 65, "qty": 1}]})).status_code == 422
        assert (await post({**ok, "items": [{"sku": f"s{i}", "qty": 1} for i in range(51)]})).status_code == 422
        assert (await post({**ok, "lat": 91})).status_code == 422
        assert (await post(ok, {"Idempotency-Key": "bad key!"})).status_code == 422
        assert (await post(ok, {"Idempotency-Key": "k" * 65})).status_code == 422


async def test_idempotency_key_is_scoped_to_the_customer(env):
    async with stack(env, inventory=False, picking=False) as c:
        body = lambda cust, qty=1: {"customer_id": cust, "store_id": "s1",  # noqa: E731
                                    "items": [{"sku": "milk", "qty": qty}]}
        h = {"Idempotency-Key": "checkout-1"}
        a = (await c.http.post("/orders", json=body("alice"), headers=h)).json()["order_id"]
        b = await c.http.post("/orders", json=body("mallory"), headers=h)
        assert b.status_code == 201 and b.json()["order_id"] != a  # another customer cannot collide
        same = await c.http.post("/orders", json=body("alice"), headers=h)
        assert same.status_code == 200 and same.json() == {"order_id": a, "status": "created"}
        diff = await c.http.post("/orders", json=body("alice", qty=5), headers=h)
        assert diff.status_code == 409
