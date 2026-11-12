"""One order = one connected trace across order, inventory and picking (real Kafka, real DB)."""
from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from darkstore_common import telemetry

from .helpers import until
from .test_saga import _eq, stack

pytestmark = pytest.mark.integration


@pytest.fixture
def spans():
    telemetry.init("it", metric_reader=InMemoryMetricReader())  # no-op if a test already did it
    exporter = InMemorySpanExporter()
    proc = SimpleSpanProcessor(exporter)
    trace.get_tracer_provider().add_span_processor(proc)
    yield exporter
    proc.shutdown()


async def test_saga_produces_one_connected_trace_rooted_at_the_order_service(env, spans):
    async with stack(env) as c:
        await c.seed()
        oid = await c.place()
        await until(lambda: _eq(c.status(oid), "picked"))
        tid = telemetry.order_trace_id(oid)
        await until(lambda: _has(spans, tid, "consume picking.completed"))
        mine = [s for s in spans.get_finished_spans() if s.context.trace_id == tid]

    names = {s.name for s in mine}
    assert {"order.create", "consume order.created", "consume inventory.reserved",
            "picking.complete", "consume picking.completed"} <= names
    ids = {s.context.span_id for s in mine}
    roots = [s for s in mine if s.parent is None]
    assert [r.name for r in roots] == ["order.create"]
    assert roots[0].context.span_id == telemetry.order_root_span_id(oid)
    orphans = [s.name for s in mine if s.parent is not None and s.parent.span_id not in ids]
    assert not orphans, f"spans whose parent is missing from the trace: {orphans}"
    groups = {s.attributes.get("messaging.consumer.group.name") for s in mine} - {None}
    assert {"inventory", "picking", "order"} <= groups


async def test_two_orders_never_share_a_trace(env, spans):
    async with stack(env) as c:
        await c.seed()
        a, b = await c.place(), await c.place()
        await until(lambda: _eq(c.status(a), "picked"))
        await until(lambda: _eq(c.status(b), "picked"))
    by_order: dict[str, set[int]] = {}
    for s in spans.get_finished_spans():
        if "order.id" in s.attributes:
            by_order.setdefault(s.attributes["order.id"], set()).add(s.context.trace_id)
    assert by_order[a] == {telemetry.order_trace_id(a)}
    assert by_order[b] == {telemetry.order_trace_id(b)}
    assert telemetry.order_trace_id(a) != telemetry.order_trace_id(b)


async def _has(spans, tid, name) -> bool:
    return any(s.context.trace_id == tid and s.name == name for s in spans.get_finished_spans())
