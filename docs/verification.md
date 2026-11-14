# Verification

Everything here was run on the author's MacBook (Docker VM ~4 GB / 8 CPUs) during the final P6/P7 pass.
Nothing is rounded in our favour; failures and re-runs are listed as they happened.

## Test suites

| Suite | Command | Result (real output line) |
|---|---|---|
| Unit | `make test` | `141 passed, 88 deselected, 1 warning in 14.84s` |
| SLO alert rules | `make test-alerts` (promtool) | `SUCCESS: 14 rules found` / `SUCCESS` |
| Integration (real Redpanda, Postgres, Redis via testcontainers) | `make test-int` | `88 passed, 141 deselected, 3 warnings in 535.36s (0:08:55)` (see note 1) |
| Playwright e2e | `make e2e` | `6 passed (1.2m)` (see note 2) |

1. **Integration.** Run with the stack stopped (`docker compose down`): an earlier attempt with the full stack up produced 63 fixture timeouts because the 4 GB Docker VM cannot host both, and was discarded. An earlier full run had one intermittent failure, `test_gate_500_concurrent_orders_zero_double_assigned_riders` (one order stuck behind the 30 s / 5 min retry tier under the compressed test timings). The test now sets a longer retry ladder (1s/2s/5s/10s/20s) and the whole suite passed in the run above; I have run it to completion once with that change, not many times, so I would still call it "passed", not "proven not flaky".
2. **e2e.** An earlier run had one failure right after a stack restart (the ETA service was replaying backlog). The run above is on a settled, freshly rebuilt stack: all six passed.

## Chaos (six scenarios, all PASS, raw JSON in `docs/chaos/`)

40 orders at 0.25/s, fault at +40 s, 300 s settle. Invariants checked after each: no lost or duplicated order, one `order.created` and one `order.delivered` per delivered order, nothing stuck, stock + reserved conserved (120000 before and after, every run), DLQ unchanged (0), WebSocket clients reach `delivered`.

| Scenario | Delivered | Create p95 ms | Retried attempts | Verdict |
|---|---|---|---|---|
| kill-picking | 40/40 | 616 | 0 | PASS |
| kill-dispatch | 40/40 | 195 | 0 | PASS |
| kill-order | 40/40 | 237 | 60 | PASS |
| kill-gateway | 40/40 | 173 | 0 | PASS |
| broker-restart | 40/40 | 185 | 0 | PASS |
| broker-crash (`kill -9`) | 40/40 | 115 | 0 | PASS |

These are from a final batch on a freshly reset stack (`make clean && make up`) running the final code. Getting here took bug fixes, listed in [ADR-0008](adr/0008-chaos-findings.md). Two batches before this one failed for reasons that were not product bugs and are worth knowing: the first because 43 orders from an earlier load run were orphaned by a Redis wipe (preflight refused to run; see ADR-0008 consequences), the second because the chaos client generated destinations outside the Whitefield store's delivery area, which the new delivery-area rule correctly rejected with 422 (the client now uses the real store coordinates; the load client had the same flaw and was fixed too). Latencies are lower than in earlier batches because earlier batches ran on a long-lived stack; I did not isolate that.

## Load (`make load`, `docs/load/results.md`)

Created/s at targets 0.5 / 1 / 1.5 / 2 / 3 / 4: 1.01 / 1.42 / 1.87 / 2.28 / 3.19 / 4.08. Delivered/s: 0.90 / 1.42 / 1.80 / 1.95 / 2.80 / 3.27. Create p95 ms: 153 / 139 / 226 / 276 / 336 / 365. DLQ 0 at every stage, 0 WebSocket failures. Delivery starts to fall behind at the 4/s stage (consumer lag peaked at 168). This run replaces an earlier, much slower one (about 2 created/s); see the README for why I do not claim a causal comparison.

## Screenshots (docs/screenshots/, captured from the running stack)

| | |
|---|---|
| Customer, one order frame by frame: picking, out for delivery, delivered (`web/e2e/readme-shots.mjs`) | [1 picking](screenshots/customer-1-picking.jpg), [2 out for delivery](screenshots/customer-2-out-for-delivery.jpg), [3 delivered](screenshots/customer-3-delivered.jpg) |
| Ops dashboard with the demo running (same script) | [ops-live.jpg](screenshots/ops-live.jpg) |
| Customer: order delivered on the map | [customer-delivered.png](screenshots/customer-delivered.png) |
| Customer: out for delivery with ETA | [customer-out-for-delivery.png](screenshots/customer-out-for-delivery.png) |
| Ops dashboard | [ops-dashboard.png](screenshots/ops-dashboard.png) |
| One order = one trace, 48 spans across order / inventory / picking / dispatch / rider_sim / eta / gateway | [trace-one-order.png](screenshots/trace-one-order.png) |
| Grafana: order funnel | [grafana-1-order-funnel.png](screenshots/grafana-1-order-funnel.png) |
| Grafana: Kafka | [grafana-2-kafka.png](screenshots/grafana-2-kafka.png) |
| Grafana: matching | [grafana-3-matching.png](screenshots/grafana-3-matching.png) |
| Grafana: ETA | [grafana-4-eta.png](screenshots/grafana-4-eta.png) |
| Grafana: WebSocket | [grafana-5-websocket.png](screenshots/grafana-5-websocket.png) |

## ETA model

LightGBM MAE 3.98 min vs calibrated heuristic 4.69 vs raw heuristic 12.00; P90 coverage 89.9% (target 90%). Simulated trips. Full report: [ml/eta-report.md](ml/eta-report.md).

## Reproduce

```bash
make up && make demo      # stack + simulator, prints URLs
make test && make test-alerts
docker compose down && make test-int && make up   # integration needs the stack stopped on a 4 GB Docker VM
make e2e
make chaos SCENARIO=broker-crash
make load
```
