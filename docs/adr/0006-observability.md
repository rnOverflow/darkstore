# ADR-0006: Observability, one trace per order
Status: accepted (P5). Dashboards, alerts and metric names: [docs/observability.md](../observability.md).

## Context
An order touches seven services over minutes, through Kafka, Postgres outboxes, Redis and timer
loops. "Where did order X get stuck?" must be answerable from one screen, and the platform's health
(funnel, lag, matching, ETA quality, sockets) must be visible without reading logs.

## Decision
- **OpenTelemetry SDK in every service, OTLP/HTTP to one Collector**, which fans out to Tempo
  (traces), Prometheus (metrics, scraped from the Collector's exporter) and Loki (logs, with trace
  and span ids attached). One `darkstore_common.telemetry` module owns setup; services call nothing
  but `order_span(...)`/the shared consumer. With `OTEL_ENDPOINT` unset every call is a no-op, so
  tests and scripts need no collector.
- **The trace id is derived from the order id** (`sha256`), and the order service starts the real root
  span `order.create` with the derived trace and span ids (custom `IdGenerator`, used once). Why: a lot
  of the saga is driven by timers (picking sweeper, dispatch matcher, rider-sim) that have no inbound
  message to inherit a context from. Any service can join the order's trace with no shared state and
  hang timer-driven spans off the known root.
- **Causality travels in Kafka headers.** `enqueue` writes the current W3C `traceparent` into the
  outbox row's headers; the relay publishes them; the consumer continues the trace from them, so the
  trace is a nested causal tree (order → inventory → picking → dispatch → rider → eta → gateway),
  not just spans sharing an id. Retry and DLQ copies carry the header. Missing or unparseable header
  → fall back to the order root.
- **Sampling is a pure function of the trace id** (`TraceIdRatioBased`), so every service reaches the
  same verdict for an order and sampled traces are never partial. Default 100%; set
  `OTEL_SAMPLE_RATIO` for load tests.
- **`rider.position` is metered but not traced** (one per busy rider per second would swamp Tempo).
- **Metrics are a named catalog** (`telemetry.Instruments`), not ad-hoc: funnel counters + stage age,
  consumer outcomes/lag/handler time/event age, assignment outcomes and wait, ETA predictions and
  scored outcomes, WebSocket connections/fan-out/drops. Dashboards and alerts are generated from /
  tested against that catalog.
- **Dashboards as code**: `scripts/gen_dashboards.py` writes five provisioned JSON dashboards;
  `scripts/check_dashboards.py` runs every query against a live Prometheus. **SLO alert rules** are
  Prometheus rules with `promtool test rules` unit tests (`make test-alerts`).

## Trade-offs and limits
- Deterministic trace ids mean an order's trace can be found from its id alone (handy for support) but
  also that anyone who knows an order id can compute the trace id. Tempo is not exposed beyond
  localhost; add auth before exposing it.
- Trace context in a header is trusted only for linking spans. Nothing routes or dedupes on it, so a
  forged `traceparent` can pollute a trace but cannot replay or reroute an event.
- One order's trace spans minutes of wall time; Tempo stitches blocks at query time. A root span that
  ends in milliseconds with children running for minutes is normal here.
- Counters that run inside a DB transaction (order funnel) can double-count if the transaction rolls
  back after the increment; rollbacks are rare and the figures are for trends, not billing.
- No Alertmanager: alerts fire and are visible in Prometheus/Grafana but notify nobody.
- Prometheus keeps 2 days (laptop). Collector metric expiry is 2 min, so a dead replica's series
  vanish quickly.
