"""Trace identity + propagation: one trace per order, parent/child preserved across Kafka headers."""
from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from darkstore_common import telemetry
from darkstore_common.envelope import Envelope

EXPORTER = InMemorySpanExporter()
READER = InMemoryMetricReader()


@pytest.fixture(scope="module", autouse=True)
def _provider():
    telemetry.init("test", span_processor=SimpleSpanProcessor(EXPORTER), metric_reader=READER)


@pytest.fixture(autouse=True)
def _clear():
    EXPORTER.clear()


def _env(order_id="o-1", event_type="inventory.reserved") -> Envelope:
    return Envelope(event_type=event_type, order_id=order_id, payload={})


def _hdrs(d: dict[str, str]) -> list[tuple[str, bytes]]:
    return [(k, v.encode()) for k, v in d.items()]


def test_trace_id_is_a_pure_function_of_the_order_id():
    assert telemetry.order_trace_id("a") == telemetry.order_trace_id("a")
    assert telemetry.order_trace_id("a") != telemetry.order_trace_id("b")
    assert 0 < telemetry.order_trace_id("a") < 2**128


def test_root_span_has_the_derived_ids_and_no_parent():
    with telemetry.start_order_trace("o-root"):
        pass
    (span,) = EXPORTER.get_finished_spans()
    assert span.context.trace_id == telemetry.order_trace_id("o-root")
    assert span.context.span_id == telemetry.order_root_span_id("o-root")
    assert span.parent is None


def test_ids_are_random_again_after_the_root_span():
    with telemetry.start_order_trace("o-x"):
        pass
    with telemetry.order_span("later", "o-y"), telemetry.order_span("inner", "o-y"):
        pass
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    assert spans["later"].context.span_id != telemetry.order_root_span_id("o-x")
    assert spans["later"].context.span_id != spans["inner"].context.span_id


def test_timer_driven_span_hangs_off_the_order_root():
    with telemetry.order_span("picking.complete", "o-2"):
        pass
    (span,) = EXPORTER.get_finished_spans()
    assert span.context.trace_id == telemetry.order_trace_id("o-2")
    assert span.parent.span_id == telemetry.order_root_span_id("o-2")


def test_order_span_nests_under_a_current_span_of_the_same_order():
    with telemetry.consume_span(_env("o-3"), "g", "t", 0, None), telemetry.order_span("work", "o-3"):
        pass
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    assert spans["work"].parent.span_id == spans["consume inventory.reserved"].context.span_id


def test_order_span_ignores_a_current_span_that_belongs_to_another_order():
    with telemetry.order_span("a", "o-4"), telemetry.order_span("b", "o-5"):
        pass
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    assert spans["b"].context.trace_id == telemetry.order_trace_id("o-5")
    assert spans["b"].parent.span_id == telemetry.order_root_span_id("o-5")


def test_header_round_trip_makes_the_consumer_a_child_of_the_producer():
    with telemetry.start_order_trace("o-6"):
        headers = telemetry.inject_headers("o-6")
    assert set(headers) == {"traceparent"}
    with telemetry.consume_span(_env("o-6"), "inventory", "t", 0, _hdrs(headers)):
        pass
    root, consumer = sorted(EXPORTER.get_finished_spans(), key=lambda s: s.start_time)
    assert consumer.context.trace_id == root.context.trace_id
    assert consumer.parent.span_id == root.context.span_id
    assert consumer.attributes["messaging.consumer.group.name"] == "inventory"


def test_chain_across_three_hops_stays_one_nested_trace():
    with telemetry.start_order_trace("o-7"):
        h1 = telemetry.inject_headers("o-7")
    with telemetry.consume_span(_env("o-7"), "inventory", "t", 0, _hdrs(h1)):
        h2 = telemetry.inject_headers("o-7")
    with telemetry.consume_span(_env("o-7", "picking.started"), "picking", "t", 0, _hdrs(h2)):
        pass
    spans = sorted(EXPORTER.get_finished_spans(), key=lambda s: s.start_time)
    assert len({s.context.trace_id for s in spans}) == 1
    assert [s.parent.span_id if s.parent else None for s in spans[1:]] == [
        spans[0].context.span_id, spans[1].context.span_id]


def test_missing_header_falls_back_to_the_order_root():
    with telemetry.consume_span(_env("o-8"), "g", "t", 0, None):
        pass
    (span,) = EXPORTER.get_finished_spans()
    assert span.parent.span_id == telemetry.order_root_span_id("o-8")


def test_inject_outside_any_span_uses_the_order_root():
    headers = telemetry.inject_headers("o-9")
    tid, sid = f"{telemetry.order_trace_id('o-9'):032x}", f"{telemetry.order_root_span_id('o-9'):016x}"
    assert headers["traceparent"].split("-")[1:3] == [tid, sid]


@pytest.mark.parametrize("raw", [b"garbage", b"00-xyz-abc-01", b"\xff\xfe", b""])
def test_forged_traceparent_cannot_break_consumption(raw):
    with telemetry.consume_span(_env("o-10"), "g", "t", 0, [("traceparent", raw)]):
        pass
    (span,) = EXPORTER.get_finished_spans()
    assert span.context.trace_id == telemetry.order_trace_id("o-10")  # ignored, root used


def test_high_frequency_position_events_are_not_traced():
    with telemetry.consume_span(_env("o-11", "rider.position"), "gateway", "t", 0, None) as span:
        assert span is None
    assert not EXPORTER.get_finished_spans()


def test_carry_headers_copies_only_the_trace_header():
    hdrs = [("traceparent", b"00-a-b-01"), ("not_before", b"1"), ("last_error", b"x")]
    assert telemetry.carry_headers(hdrs) == [("traceparent", b"00-a-b-01")]
    assert telemetry.carry_headers(None) == []


def test_error_status_is_recorded_on_the_span():
    with telemetry.consume_span(_env("o-12"), "g", "t", 1, None) as span:
        telemetry.mark_error(span, "ValueError: boom")
    (s,) = EXPORTER.get_finished_spans()
    assert s.status.status_code == trace.StatusCode.ERROR
    assert s.attributes["event.attempt"] == 1


def test_metrics_are_exported_with_their_attributes():
    ins = telemetry.instruments()
    ins.events.add(1, {"group": "g", "event_type": "e", "outcome": "ok"})
    ins.order_age.record(2.5, {"status": "picked"})
    telemetry.set_lag("g", "darkstore.order.events", 7)
    data = READER.get_metrics_data()
    names = {m.name: m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert {"darkstore.events", "darkstore.order.age", "darkstore.consumer.lag"} <= set(names)
    lag = names["darkstore.consumer.lag"].data.data_points[0]
    assert lag.value == 7 and dict(lag.attributes) == {"group": "g", "topic": "darkstore.order.events"}
    telemetry.clear_lag("g")


def test_a_valid_header_naming_another_orders_trace_is_ignored():
    with telemetry.start_order_trace("victim"):
        stolen = telemetry.inject_headers("victim")
    EXPORTER.clear()
    with telemetry.consume_span(_env("attacker-order"), "g", "t", 0, _hdrs(stolen)):
        pass
    (span,) = EXPORTER.get_finished_spans()
    assert span.context.trace_id == telemetry.order_trace_id("attacker-order")
    assert span.context.trace_id != telemetry.order_trace_id("victim")


def test_root_span_exception_text_is_not_exported():
    with pytest.raises(RuntimeError):
        with telemetry.start_order_trace("o-leak"):
            raise RuntimeError("INSERT ... [parameters: ('customer-secret', ...)]")
    (span,) = EXPORTER.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert "customer-secret" not in (span.status.description or "")
    assert not span.events                      # no recorded exception event / stack trace


def test_envelope_ids_are_bounded():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Envelope(event_type="x", order_id="o" * 65)
    with pytest.raises(ValidationError):
        Envelope(event_type="x", order_id="o", event_id="e" * 65)
