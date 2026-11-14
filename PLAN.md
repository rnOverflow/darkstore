# DarkStore Implementation Plan

> Execution: native, phase by phase, with specialist subagents dispatched in parallel (git worktrees) for independent streams. Each phase closes only after `superpowers:verification-before-completion` (real command output) and a reviewer pass.

**Goal:** real-time quick-commerce order + ETA platform, fully working on a laptop via Docker Compose.
**Spec:** `docs/superpowers/specs/2026-11-12-darkstore-design.md`

## Global constraints
- Python 3.12, uv workspace, one Dockerfile (`SERVICE` build arg).
- Conventional commits, one per task. Nothing "done" without verification output.
- No performance number in docs unless measured on this machine (8 CPU / 8 GB).
- Infra memory-capped for an 8 GB host.

## Review focus (failure modes the tests must pin)
1. Duplicate event delivery -> side effect applied once.
2. Out-of-order events for one order (e.g. `picking.completed` before `picking.started`) -> state machine rejects/parks, no corruption.
3. Concurrent rider claims on one rider / one order -> exactly one winner.
4. Broker restart mid-flow -> no lost or duplicated orders (outbox replays).
5. WS client reconnect with stale `last_event_id` -> gap-free resume; slow client never blocks status events.

## Phases

### P0 Repo, conventions, compose skeleton — gate: `make up` all green
- T0.1 git repo, development notes, PLAN.md, spec, ADR dir
- T0.3 uv workspace (`packages/common`, `services/`), Dockerfile base, Makefile
- T0.4 compose: Redpanda(+SR) + Console, Postgres, Redis, OTel Collector, Tempo, Prometheus, Loki, Grafana, MLflow, healthchecks, provisioned datasources
- T0.5 `scripts/healthcheck.sh` + `make up` waits for healthy; common package smoke tests (topic registry, envelope)

### P1 Order -> inventory -> picking saga — gate: integration tests incl. duplicate + out-of-order pass
- T1.1 ADRs (topics, envelope, idempotency, saga) · T1.2 envelope + outbox relay + idempotent consumer base (TDD)
- T1.3 order-service REST + outbox · T1.4 inventory reservations + compensation · T1.5 picking workflow
- T1.6 retry/DLQ topics + replay tool · T1.7 testcontainers integration suite

### P2 Simulator, rider-sim, H3 matching — gate: 500 concurrent orders, zero double-assigned riders
- T2.1 H3 index + k-ring search · T2.2 Lua atomic claim + race tests · T2.3 scoring · T2.4 dispatch-service + reassignment on timeout
- T2.5 simulator (city, stores, demand curves) · T2.6 rider-sim GPS pings · T2.7 500-order assertion test

### P3 ETA — gate: LightGBM beats heuristic on held-out MAE; table reported
- T3.1 trip simulator -> Parquet · T3.2 features · T3.3 train P50/P90 + MLflow run + retrain script · T3.4 eval table + P90 coverage
- T3.5 eta-service with flag + fallback, re-predict on state change

### P4 Realtime + UIs — gate: Playwright e2e order -> delivered, screenshot
- T4.1 WS protocol + gateway (auth, snapshot, resume, backpressure) · T4.2 Redis fan-out across replicas
- T4.3 customer UI · T4.4 ops-api + ops UI · T4.5 Playwright e2e

### P5 Observability — gate: screenshot of one order trace spanning all services  ✅ done (see docs/observability.md, ADR-0006)
- T5.1 OTel in common lib + Kafka header propagation · T5.2 five Grafana dashboards as code · T5.3 SLO alert rules

### P6 Chaos + load — gate: no lost/duplicated orders; measured throughput/latency documented  ✅ done (docs/chaos, docs/load, ADR-0008)
- T6.1 kill consumer · T6.2 kill gateway replica · T6.3 broker restart mid-flow · T6.4 Locust ramp + results table

### P7 Security, review, docs  ✅ done (ADR-0007, README, docs/verification.md, docs/runbook.md; open items listed in ADR-0007 'Known deviations')
Backlog carried from P1 security review (not yet done): authn on all `/orders*` routes with ownership checks and server-assigned `customer_id`; rate limits + body size cap; hide `/docs`; Kafka SASL/TLS + per-service ACLs; per-service Postgres roles (all services share one superuser today); pin images by digest; outbox/processed_events purge; single-relay advisory lock; cap `attempts` (a never-picked-up order can cycle forever) and add retention for `assignment_log`/`assignments`; Redis ACLs (requirepass is done); the flat pickup timeout should derive from rider ETA; customer cancel after pickup is only half-handled (documented); batching potential is implemented in the scorer but riders are single-order (term is always 0).
From the P4 reviews (open): customer authn so order creation and `GET/POST /orders/{id}` are not bearer-by-order-id; token revocation (`jti` + denylist) and key rotation (`kid`), or asymmetric signing so only the order service can mint; the ops token is a 4 h bearer printed to the terminal; `limit_req`/real-IP handling behind a proxy (nginx rate-limits by remote addr); HSTS once TLS exists; a separate lossy consumer for `rider.positions` so a position flood cannot delay statuses; close tracking sockets a grace period after a terminal status; cross-topic rank can swallow an intermediate status from the history list (the stepper still shows it done).
From the P3 review: MLflow server is v2.16.0 with no auth (upgrade, auth proxy, digest-pin); the model bundle digest is verified but a writer of both meta.json and the pickles can still run code (sign bundles); retention for `eta.order_state`/`eta.outcomes`; bound `occurred_at` so forged timestamps cannot poison `outcomes.actual_min`; stream `eta.updated` to clients with a sequence number (P4).
From the P5 reviews (open): trace ids are derivable from order ids and Tempo/Loki/Prometheus/Collector are unauthenticated inside the compose network (fine on localhost; add auth/network policy before exposing); no Alertmanager so alerts notify nobody; nginx caches upstream IPs (restart `web` after a replica is recreated; use a resolver or nginx >= 1.27.3 `resolve` in P6); no admission control, so a demo-rate simulator builds an unbounded dispatch backlog (hundreds of pending orders) until orders time out; ETA/order-funnel counters are bumped inside handler transactions; `ws_dropped` counts only the live route path.
- T7.1 securitymaxxing audit/authn/api/secrets/deps/ship-check + fixes · T7.2 code-review pass · T7.3 makedoc README, diagram, ADR index, runbook, measured-numbers table
