# DarkStore development notes

Real-time quick-commerce order + ETA platform. Runs on a laptop via Docker Compose. See `PLAN.md` for phases and gates, `docs/superpowers/specs/` for the design, `docs/adr/` for decisions.

## Stack
Python 3.12 · FastAPI · aiokafka · pydantic v2 · SQLAlchemy + Alembic · Redpanda (+Schema Registry, Console) · Postgres 16 · Redis 7 · h3-py · LightGBM + MLflow · React + Vite + TS + MapLibre + Zustand · OpenTelemetry -> Collector -> Tempo / Prometheus / Loki -> Grafana · pytest, testcontainers, Schemathesis, Locust, Playwright.

## Layout
```
packages/common/     shared lib: envelope, outbox, idempotent consumer, tracing, settings, topics
services/<name>/     order, inventory, picking, dispatch, rider_sim, eta, tracking_gateway, simulator, ops_api
web/                 customer-ui, ops-ui (React)
infra/               otel, tempo, prometheus, loki, grafana (provisioned, dashboards as code)
scripts/             healthcheck, replay, chaos, seed
tests/               cross-service integration / e2e / load
docs/                adr/, superpowers/specs, runbook
```

## Commands
- `make up` / `make down` — start/stop infra (+ services as they exist); `up` waits until healthy
- `make health` — per-component health check
- `make test` — unit tests (`make test-int` for testcontainers integration)
- Running the stack needs only Docker + make: the `web` image builds the React apps (multi-stage) and `make train` / `make retrain` run the one-shot `trainer` container (compose profile `tools`). `make web` is only for front-end work on the host.
- `make e2e` — Playwright against the running stack (customer journey, reconnect, ops dashboard); screenshots land in `docs/screenshots/`
- `make train` / `make retrain` — ETA model (see `docs/ml/README.md`)
- `make demo` — seed city, run simulator, print URLs (customer UI and a signed ops URL on http://localhost:8088)
- `make dashboards` — regenerate the Grafana dashboards from `scripts/gen_dashboards.py`; `make test-alerts` — `promtool` unit tests for the SLO alert rules (see `docs/observability.md`)
- `make load` — Locust ramp; `make chaos SCENARIO=<name>` — one fault scenario (see `docs/chaos/`, ADR-0008)
- `make clean` — also required once after P7: per-service Postgres roles change only on a fresh volume
- Run `make test-int` with the stack stopped (`docker compose down`): the 4 GB Docker VM cannot host both
- `uv sync` installs the workspace; `uv run pytest <path>` runs a subset

## Local gotchas
- `make env` generates `.env` with random credentials (compose refuses to start without it). All published ports bind 127.0.0.1; compose Postgres is on host port **5433** (5432 is the host's own Postgres).
- macOS marks uv's editable `.pth` files hidden and Python then skips them (ImportError). `make sync` clears the flag; use `uv run --no-sync` afterwards.
- After `docker compose up` recreates a service, restart `web`: nginx caches upstream IPs at startup, so `/api/*` 502s until it is restarted (the nginx `resolve` fix landed in P6, restarting `web` is still the safe move).
- The simulated clock's epoch lives in Redis and is set when the stack first boots, so `make demo` should follow `make up` promptly; `SIM_START_HOUR=16` is relative to that boot, not to the simulator's start.
- MapLibre 6 starts its GeoJSON worker from a file Vite does not emit by itself: `web/src/shared/mapStyle.ts` imports it as an asset and nginx serves `.mjs` as JavaScript. Without both, the ops map shows no riders or heat cells and the customer trail line is missing (the e2e ops test checks the console for worker errors).
- MLflow runs with `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false`: with 3.17's default job workers it burned ~2 CPUs idle and starved the order pipeline.
- Docker VM has ~4 GB: keep containers memory-capped. Redpanda needs `topic_memory_per_partition` lowered (done by the `redpanda-config` one-shot service) or retry/DLQ topics exceed its partition cap.

## Conventions
- Conventional commits, one per task. Small focused files. Type hints everywhere; pydantic v2 models for all event payloads.
- Domain logic is written test-first.
- Events: partition key = `order_id`; envelope has `event_id`, `schema_version`; consumers idempotent by `event_id`; additive-only schema changes within a major version.
- Trace context goes in Kafka headers (`traceparent`); the trace id of an order is derived from its order id (ADR-0006). Timer-driven work uses `telemetry.order_span`. New metrics go in `telemetry.Instruments` (and a dashboard/alert if operators need them); never put exception text with SQL params into spans.
- Config via env vars; no secrets in git (`.env.example` only).

## Definition of done
A task/phase is done only when: code runs, tests pass, the gate command was executed and its **real output** was seen (and pasted in the report), a security review and a code review have passed for the phase, and docs/ADRs are updated. Never claim a performance number that was not measured on this machine. If a check cannot run, say so; do not skip it silently.
