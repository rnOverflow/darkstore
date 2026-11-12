# DarkStore design spec

## Understanding (confirmed by the brief)
- **Outcome:** a real-time quick-commerce order + ETA platform running end to end on one laptop via Docker Compose. `make up && make demo` boots everything, seeds a city, runs the simulator and prints UI URLs.
- **Audience:** a portfolio / interview showpiece at Swiggy/Zepto scale-thinking; every claim must be backed by measured output.
- **Success:** each phase P0–P7 ends with running code, passing tests and a visible demo. Performance numbers are only reported if measured.
- **Fixed stack (not relitigated):** Python 3.12, FastAPI, aiokafka, pydantic v2, SQLAlchemy + Alembic, uv workspace, Redpanda + Schema Registry, Postgres 16, Redis 7, h3-py, LightGBM + MLflow, React/Vite/TS/MapLibre/Zustand, OTel -> Collector -> Tempo/Prometheus/Loki/Grafana, pytest/testcontainers/Schemathesis/Locust/Playwright.

## Assumptions (to correct if wrong)
- Host is Apple Silicon, 8 CPU / 8 GB RAM. Docker Desktop VM gets ~6 GB, so infra containers are memory-capped (Redpanda `--smp 1 --memory 512M`, Loki/Tempo/Prom single-binary, MLflow on SQLite).
- Services share one Postgres instance with a schema per service (orders, inventory, picking, dispatch). Logical isolation, not physical. Noted as a trade-off in the README.
- Event serialization: **JSON Schema** registered in Redpanda Schema Registry (cheaper to debug than Avro in Console). Envelope fields: `event_id`, `event_type`, `schema_version`, `occurred_at`, `order_id`, `trace` headers, `payload`.
- Single city, ~6 dark stores, a few hundred riders, simulated clock speed-up factor configurable.

## Architecture
Services (one image, `SERVICE` selects entrypoint): order, inventory, picking, dispatch, rider-sim, eta, tracking-gateway, simulator, ops-api. Shared lib `packages/common` (envelope, outbox, consumer base with idempotency, tracing, settings, topic registry).

**Ordering and delivery:** partition key = `order_id`; producers write via transactional outbox (order-service, and each service for its own events); consumers dedupe via `processed_events(consumer, event_id)` in the same DB transaction as their side effect; failures go to `<topic>.retry.{5s,30s,5m}` then `<topic>.dlq`. Saga: order-service orchestrates by reacting to events; timeouts/cancel publish `inventory.release` compensation.

**Matching:** Redis sets per H3 res-8 cell of idle riders; k-ring expansion; score blend of ETA-to-store, rating, batching potential, idle-time fairness; claim by Lua script (SET rider lock + remove from idle set atomically, fenced by order_id).

**ETA:** simulator writes Parquet trips; LightGBM quantile (0.5, 0.9) vs heuristic baseline, time-based split; served in-process behind flag `ETA_MODEL_ENABLED` with heuristic fallback; re-predicted on each state change and emitted as `eta.updated`.

**Realtime:** gateway replicas consume events, publish to Redis pub/sub channel per order; each replica fans out to its sockets; per-socket bounded queue (drop-oldest for position events, status events never dropped); `last_event_id` resume from Redis stream per order.

**Observability:** OTel SDK in every service; W3C `traceparent` in Kafka headers; one order = one trace. Dashboards as JSON under `infra/grafana/dashboards`.

## Testing
TDD for domain logic; testcontainers for integration; duplicate/out-of-order/redelivery tests in P1; 500-concurrent-order no-double-assign assertion in P2; Playwright e2e in P4; chaos scripts + Locust in P6.

## Out of scope
OSRM (stretch), real payments, multi-region deployment, auth beyond signed demo tokens (P7 reviews and hardens).
