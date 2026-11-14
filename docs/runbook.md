# Runbook

Everything runs from `docker compose` on one machine. Start with `make health`; it names the failing component.

## Start, stop, reset
| Task | Command |
|---|---|
| Start (builds, waits for health) | `make up` |
| Demo (trains ETA model if missing, starts the order simulator, prints URLs) | `make demo` |
| Stop, keep data | `make down` |
| Wipe all volumes (required after changing DB roles/schema) | `make clean` |
| Remove testcontainers left by an interrupted `make test-int` | `make test-clean`, then `docker rm -f <names>` |

## Alerts (infra/prometheus/rules/slo.yml, tested by `make test-alerts`)
Every alert has a `runbook` annotation. First look: Grafana http://localhost:3000, then `docs/observability.md`.

| Symptom | Likely cause | Check / action |
|---|---|---|
| Orders stuck in `created` | order consumers lag or inventory down | Grafana "Kafka" dashboard (consumer lag); `docker compose logs inventory` |
| Orders stuck in `picked` | no idle rider (`FleetStarved`) | Matching dashboard; riders are the delivery bottleneck (see docs/load/results.md). Add riders or slow order intake |
| DLQ growing | a poison event or an out-of-order bug | Redpanda Console, topic `*.dlq`; fix the cause, then replay with `scripts/replay` |
| ETA prediction error high | stale model | `make retrain` (promotes only if it beats the live model) |
| WebSocket drops/overflow | slow clients or gateway CPU | WebSocket dashboard; the other replica keeps serving, clients reconnect and resume |
| One service unhealthy | crash loop | `docker compose logs <svc>`; services restart automatically and consumers resume from committed offsets |
| Whole stack slow, containers OOM-killed | Docker VM has ~4 GB | `docker stats`; remove leftover testcontainers; stop `mlflow` if not training |

## Tracing one order
Trace id = first 16 bytes of sha256("trace:" + order_id). Grafana → Explore → Tempo → paste the 32-hex id (script: `web/e2e/grafana-shots.mjs`).

## Secrets and tokens
- `make env` writes `.env` (git-ignored) with random credentials. Rotate by deleting `.env`, running `make env` and `make clean`.
- Changing `TOKEN_SECRET` invalidates every issued token; restart all services.
- Ops dashboard URL with a 4 h token: `uv run --no-sync python scripts/ops_token.py`.

## Chaos
`make chaos SCENARIO=kill-picking|kill-dispatch|kill-order|kill-gateway|broker-restart|broker-crash`. It checks that no acknowledged order is lost or duplicated, DLQ is unchanged, and WebSocket clients reach `delivered`. Run below the fleet capacity (see docs/chaos/).
