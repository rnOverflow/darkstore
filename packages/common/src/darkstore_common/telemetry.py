"""OpenTelemetry wiring: ONE trace per order across every service, plus the metrics catalog.

Tracing model
-------------
* The trace id of an order is derived from its order id (sha256), so any service can add spans to
  the order's trace without shared state, including timer-driven work (picking sweeper, dispatch
  matcher, rider-sim) that has no inbound message to inherit a context from.
* The order service starts the trace's real root span (``order.create``) with ids forced to the
  derived values. Timer-driven spans parent to that root (``order_span``).
* Every outbox row carries a W3C ``traceparent`` header (``enqueue`` injects it), and the consumer
  continues the trace from it, so the causal chain order -> inventory -> picking -> dispatch ->
  rider -> eta -> gateway is nested, not just correlated.
* Trust: the header only decides which trace a span joins. Routing and idempotency never read it,
  so a forged ``traceparent`` can at worst pollute traces (it cannot reroute or replay anything).
* Without an endpoint (tests, local scripts) every call is a cheap no-op.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import os
import socket
from collections.abc import Iterable, Iterator
from functools import lru_cache

from opentelemetry import context as otx
from opentelemetry import metrics, propagate, trace
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import (NonRecordingSpan, Span, SpanContext, SpanKind, Status, StatusCode,
                                 TraceFlags)

log = logging.getLogger("darkstore.telemetry")
TRACEPARENT = "traceparent"
# Far too frequent to trace (one per busy rider per second); metrics still count them.
UNTRACED_EVENTS = frozenset({"rider.position"})

_forced_ids: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar(
    "darkstore_forced_ids", default=None)


# --------------------------------------------------------------------------- ids and contexts

def order_trace_id(order_id: str) -> int:
    return int.from_bytes(hashlib.sha256(b"trace:" + order_id.encode()).digest()[:16], "big") or 1


def order_root_span_id(order_id: str) -> int:
    return int.from_bytes(hashlib.sha256(b"root:" + order_id.encode()).digest()[:8], "big") or 1


def order_context(order_id: str) -> otx.Context:
    """Context whose current span is the order's root span (for work with no inbound message)."""
    sc = SpanContext(order_trace_id(order_id), order_root_span_id(order_id), is_remote=True,
                     trace_flags=TraceFlags(TraceFlags.SAMPLED))
    return trace.set_span_in_context(NonRecordingSpan(sc), otx.Context())


class _IdGenerator(RandomIdGenerator):
    """Random ids, except for the one root span the order service asks for explicitly."""

    def generate_trace_id(self) -> int:
        forced = _forced_ids.get()
        return forced[0] if forced else super().generate_trace_id()

    def generate_span_id(self) -> int:
        forced = _forced_ids.get()
        return forced[1] if forced else super().generate_span_id()


# --------------------------------------------------------------------------- setup

_initialised = False
_log_provider = None
_lag: dict[tuple[str, str], int] = {}


def _lag_callback(_options) -> Iterable:
    for (group, topic), v in list(_lag.items()):
        yield metrics.Observation(v, {"group": group, "topic": topic})


def init(service: str, *, endpoint: str = "", sample_ratio: float = 1.0,
         metric_interval: float = 5.0, span_processor=None, metric_reader=None) -> None:
    """Configure tracing + metrics (+ logs) for this process. Idempotent; no-op without a target."""
    global _initialised
    if _initialised or not (endpoint or span_processor or metric_reader):
        return
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    _initialised = True
    resource = Resource.create({"service.name": service, "service.namespace": "darkstore",
                                "service.instance.id": socket.gethostname()})
    # TraceIdRatioBased (not parent-based): every service reaches the same verdict for a given
    # order, because the decision is a pure function of the order-derived trace id.
    tp = TracerProvider(resource=resource, sampler=TraceIdRatioBased(sample_ratio),
                        id_generator=_IdGenerator())
    readers = [metric_reader] if metric_reader else []
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(f"{endpoint}/v1/traces")))
        readers.append(PeriodicExportingMetricReader(
            OTLPMetricExporter(f"{endpoint}/v1/metrics"),
            export_interval_millis=int(metric_interval * 1000)))
        _install_log_handler(resource, endpoint)
    if span_processor:
        tp.add_span_processor(span_processor)
    trace.set_tracer_provider(tp)
    seconds = ExplicitBucketHistogramAggregation(
        (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600))
    minutes = ExplicitBucketHistogramAggregation((0.25, 0.5, 1, 2, 3, 4, 5, 7.5, 10, 15, 20, 30, 60))
    views = [View(instrument_name=n, aggregation=seconds)
             for n in ("darkstore.event.handle", "darkstore.event.age", "darkstore.order.age",
                       "darkstore.assignment.wait", "darkstore.match.duration",
                       "darkstore.ws.fanout")]
    views.append(View(instrument_name="darkstore.eta.abs_error", aggregation=minutes))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers, views=views))
    metrics.get_meter("darkstore").create_observable_gauge(
        "darkstore.consumer.lag", callbacks=[_lag_callback], unit="{message}",
        description="Messages the consumer group has not processed yet (log end - position)")


def _install_log_handler(resource, endpoint: str) -> None:
    """Ship logs (with trace/span ids attached) to Loki through the collector. Best effort."""
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(f"{endpoint}/v1/logs")))
        set_logger_provider(lp)
        global _log_provider
        _log_provider = lp
        handler = LoggingHandler(level=logging.INFO, logger_provider=lp)
        # the exporter's own "collector unreachable" warnings must not be re-exported as logs
        handler.addFilter(lambda r: not r.name.startswith("opentelemetry"))
        logging.getLogger().addHandler(handler)
    except Exception:  # noqa: BLE001 - logs are optional; traces and metrics must not depend on them
        log.warning("log export disabled", exc_info=True)


def init_from_settings(service: str, settings) -> None:
    init(service, endpoint=settings.otel_endpoint.rstrip("/"),
         sample_ratio=settings.otel_sample_ratio, metric_interval=settings.otel_metric_interval)


def shutdown() -> None:
    """Flush buffered spans/metrics (called on service stop)."""
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider(), _log_provider):
        if provider is not None:
            with contextlib.suppress(Exception):
                provider.shutdown()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- propagation

def _tracer():
    return trace.get_tracer("darkstore")


def inject_headers(order_id: str | None = None) -> dict[str, str]:
    """``traceparent`` for the current span, else for the order's root (timer-driven emitters)."""
    carrier: dict[str, str] = {}
    ctx = otx.get_current()
    if not trace.get_current_span(ctx).get_span_context().is_valid and order_id:
        ctx = order_context(order_id)
    propagate.inject(carrier, context=ctx)
    return {k: v for k, v in carrier.items() if k == TRACEPARENT}


def extract_context(headers: Iterable[tuple[str, bytes]] | None, order_id: str) -> otx.Context:
    """The producer's context from Kafka headers; the order's root when there is none."""
    carrier: dict[str, str] = {}
    for k, v in headers or ():
        if k == TRACEPARENT:
            with contextlib.suppress(UnicodeDecodeError):
                carrier[k] = v.decode()
    ctx = propagate.extract(carrier)
    sc = trace.get_current_span(ctx).get_span_context()
    # a header naming ANY other trace is ignored: it could only be a bug or a forgery, and honouring
    # it would let a producer inject spans into someone else's order trace
    if sc.is_valid and sc.trace_id == order_trace_id(order_id):
        return ctx
    return order_context(order_id)


def carry_headers(headers: Iterable[tuple[str, bytes]] | None) -> list[tuple[str, bytes]]:
    """Trace header to copy onto a retry / DLQ copy of a message."""
    return [(k, v) for k, v in headers or () if k == TRACEPARENT]


# --------------------------------------------------------------------------- spans

@contextlib.contextmanager
def start_order_trace(order_id: str, name: str = "order.create", **attrs) -> Iterator[Span]:
    """The real root span of an order's trace (order service, once per order)."""
    token = _forced_ids.set((order_trace_id(order_id), order_root_span_id(order_id)))
    try:
        span = _tracer().start_span(name, context=otx.Context(), kind=SpanKind.SERVER,
                                    attributes={"order.id": order_id, **attrs})
    finally:
        _forced_ids.reset(token)
    # record_exception=False: the exception text may carry SQL parameters; keep only the type name
    with trace.use_span(span, end_on_exit=True, record_exception=False, set_status_on_exception=False):
        try:
            yield span
        except BaseException as e:
            span.set_status(Status(StatusCode.ERROR, type(e).__name__))
            raise


@contextlib.contextmanager
def order_span(name: str, order_id: str, **attrs) -> Iterator[Span]:
    """A span in the order's trace for work not triggered by a message (timers, loops).

    Inside another span of the same trace it nests under it; otherwise it hangs off the root."""
    cur = trace.get_current_span().get_span_context()
    ctx = None if (cur.is_valid and cur.trace_id == order_trace_id(order_id)) else order_context(order_id)
    with _tracer().start_as_current_span(name, context=ctx, kind=SpanKind.INTERNAL,
                                         attributes={"order.id": order_id, **attrs}) as span:
        yield span


@contextlib.contextmanager
def consume_span(env, group: str, topic: str, attempt: int,
                 headers: Iterable[tuple[str, bytes]] | None) -> Iterator[Span | None]:
    if env.event_type in UNTRACED_EVENTS:
        yield None
        return
    ctx = extract_context(headers, env.order_id)
    with _tracer().start_as_current_span(
            f"consume {env.event_type}", context=ctx, kind=SpanKind.CONSUMER,
            attributes={"order.id": env.order_id, "messaging.system": "kafka",
                        "messaging.destination.name": topic, "messaging.consumer.group.name": group,
                        "messaging.message.id": env.event_id, "event.type": env.event_type,
                        "event.attempt": attempt},
            record_exception=False, set_status_on_exception=False) as span:
        yield span


def mark_error(span: Span | None, description: str) -> None:
    """Flag the span failed. Pass a SANITISED description (consumer._describe): exception text can
    embed SQL bound parameters (customer ids, items), which must not reach the trace backend."""
    if span is not None and span.is_recording():
        span.set_status(Status(StatusCode.ERROR, description[:200]))


# --------------------------------------------------------------------------- metrics catalog

class Instruments:
    """Every metric the dashboards and alerts use (names are the contract, see docs/observability)."""

    def __init__(self) -> None:
        m = metrics.get_meter("darkstore")
        self.events = m.create_counter("darkstore.events", unit="{event}",
            description="Consumed events by outcome: ok | duplicate | retry | dlq")
        self.event_handle = m.create_histogram("darkstore.event.handle", unit="s",
            description="Handler duration per consumed event")
        self.event_age = m.create_histogram("darkstore.event.age", unit="s",
            description="Age of an event (now - occurred_at) when its consumer starts on it")
        self.order_transitions = m.create_counter("darkstore.order.transitions", unit="{order}",
            description="Orders entering each status (the order funnel)")
        self.order_age = m.create_histogram("darkstore.order.age", unit="s",
            description="Real seconds from order creation until it entered a status")
        self.assignments = m.create_counter("darkstore.assignments", unit="{assignment}",
            description="Dispatch outcomes: assigned | no_rider | timed_out")
        self.assignment_wait = m.create_histogram("darkstore.assignment.wait", unit="s",
            description="Seconds an order waited for a rider (ready for dispatch -> assigned)")
        self.match_duration = m.create_histogram("darkstore.match.duration", unit="s",
            description="Duration of one rider-matching search")
        self.eta_predictions = m.create_counter("darkstore.eta.predictions", unit="{prediction}",
            description="ETA predictions by source: model | heuristic (fallback)")
        self.eta_outcomes = m.create_counter("darkstore.eta.outcomes", unit="{order}",
            description="Delivered orders scored against their first ETA; covered = within P90")
        self.eta_abs_error = m.create_histogram("darkstore.eta.abs_error", unit="min",
            description="|first P50 prediction - actual| in simulated minutes")
        self.eta_early = m.create_counter("darkstore.eta.early", unit="min",
            description="Sum of (actual - first P50) over orders that arrived LATER than the P50 promise "
                        "(promised too early)")
        self.eta_late = m.create_counter("darkstore.eta.late", unit="min",
            description="Sum of (first P50 - actual) over orders that arrived EARLIER than the P50 promise")
        self.ws_connections = m.create_up_down_counter("darkstore.ws.connections", unit="{connection}",
            description="Open tracking WebSocket connections by role")
        self.ws_dropped = m.create_counter("darkstore.ws.dropped", unit="{message}",
            description="Droppable (position/eta) messages discarded for slow clients")
        self.ws_overflow = m.create_counter("darkstore.ws.overflow", unit="{connection}",
            description="Connections cut because the client stopped reading")
        self.ws_fanout = m.create_histogram("darkstore.ws.fanout", unit="s",
            description="Seconds an event waited between reaching the gateway hub and the socket write")


@lru_cache(maxsize=1)
def instruments() -> Instruments:
    return Instruments()


def set_lag(group: str, topic: str, lag: int) -> None:
    _lag[(group, topic)] = lag


def clear_lag(group: str) -> None:
    for k in [k for k in _lag if k[0] == group]:
        del _lag[k]


def service_name_from(module: str) -> str:
    """``darkstore_order.__main__`` -> ``order``."""
    return os.environ.get("OTEL_SERVICE_NAME") or module.split(".")[0].removeprefix("darkstore_")
