# ADR-0008: What the chaos runs found

Status: accepted. Measured on one laptop (Docker VM ~4 GB / 8 CPU), 120 simulated riders, 40 orders at 0.25/s,
fault injected at +40 s, 300 s settle. Harness: `tests/chaos/run.py`, logs and JSON in `docs/chaos/`.

## Invariants checked after every scenario
No lost or duplicated orders; exactly one `order.created` and one `order.delivered` per delivered order; no order
stuck in a non-terminal state; assignment-log replay matches final state; stock + reserved is conserved; DLQ
unchanged; every WebSocket tracker reaches `delivered`.

## Scenarios
kill a consumer (picking, dispatch, order), kill a gateway replica, `docker restart` the broker, `kill -9` the broker.
Results: see `docs/chaos/*.json` (the table in the README is generated from them).

## Findings (each one was a real bug, found by a failing run)
1. **Dispatch head-of-line blocking.** `match_pending` counted consecutive misses and stopped, so a few old orders at a
   rider-less store starved every younger order at other stores. Fix: skip a store for the rest of the tick once one
   search for it finds nobody. Regression test `test_orders_for_a_store_with_no_riders_do_not_block_other_stores`.
2. **Fleet drift.** Riders finished a delivery wherever the customer lived and stayed there. Whitefield (store-3) is
   ~12 km from the other stores, so its riders migrated away and its orders stayed `pending` forever. Fix: idle riders
   cycle back to a home store (`geo.return_to_base`, `rider_sim.home_of`). Unit test in `packages/common/tests/test_geo.py`.
3. **Redpanda write caching.** `--mode=dev-container` enables `write_caching_default`: acks (even `acks=all`) are
   sent before fsync, so `kill -9` loses up to ~100 ms of *acknowledged* records. The outbox had marked those rows
   published, so the events were gone. Fix: `rpk cluster config set write_caching_default false`, pinned by the `redpanda-config`
   one-shot service, which every app waits for (it is a cluster property; `--set` on the command line is ignored).

## Consequences
- Durable acks cost some broker throughput; irrelevant at this scale, would be re-evaluated against replication at real scale.
- Orders whose events were lost before finding 3 were stranded; a production fix would add an outbox-vs-broker
  reconciler. Not built; recorded as a known gap.
- **Redis is not persistent (`--save "" --appendonly no`) and is not in the chaos set.** Found while preparing the final
  run: 43 orders from an earlier load run sat in `out_for_delivery` for hours, because a Docker restart had wiped Redis
  and with it the riders' in-flight state. Postgres still said `picked_up`, but no rider knew about the order any more,
  so it could never be delivered (the preflight "system is not quiet" guard caught it; the stack was reset with
  `make clean`). Mitigation at real scale: Redis with AOF/replicas, plus a reconciler that re-creates a rider
  hold for any `picked_up` assignment nobody carries. Not built; recorded as a known gap.
