# ADR-0004: Rider matching, atomic claim, dispatch ownership
Status: accepted (P2)
## Context
Hundreds of orders compete for a small fleet; several dispatch replicas may match at once. A rider must never hold two orders and an order must never hold two riders.
## Decision
- **Index:** Redis. `rider:{id}` hash, `idle:{h3_res8_cell}` sets of idle riders, `order_rider:{oid}` fence. Search = H3 k-ring expansion from the store cell until >= 8 idle riders (max 8 rings).
- **Score** (lower is better): `eta_min + 2.0*(5-rating) - 0.1*min(idle_min,30) - 3.0*batch_potential`; ties by rider_id. ETA = haversine x 1.4 road factor / rider speed.
- **Claim:** one Lua script. It succeeds only if the order has no other rider, the rider is `idle`, and SREM from the idle set removed them. Re-claim of the same (rider, order) is an idempotent success, so a crash between claim and DB commit is recoverable. A lost race moves on to the next-ranked candidate.
- **Ownership:** dispatch is the only component that claims AND releases riders. Rider-sim only emits `rider.picked_up` / `rider.delivered`; dispatch frees the rider when it processes `rider.delivered`. Every claim/release writes an append-only `assignment_log` row *before* the Redis change that follows it, so replaying the log by id proves non-overlap.
- **Pending queue:** orders wait in `dispatch.assignments` (status `pending`); a matcher loop assigns oldest first using `FOR UPDATE SKIP LOCKED`, so replicas never double-process an order and an empty fleet just means waiting (no retry-topic abuse).
- **Reassignment:** assignment gets a pickup deadline. On timeout the rider is benched (`cooldown`) and the order returns to `pending`. Cancel releases the rider immediately; a cancel that arrives first leaves a tombstone.
## Amendments after P2 review
- **One DB transaction per order** when matching, so a failed commit can strand at most that order's claim; cancel releases *whoever Redis says holds the order* (not just what the DB row says); a **reconciler** releases claims older than a grace period that the DB does not back (Redis and Postgres cannot commit together).
- **Rider-sim phases are retryable** (`to_store -> at_store -> to_customer -> at_customer -> done`): every emit is repeated on the next tick if the phase did not advance; consumers ignore duplicates.
- The pickup-timeout sweeper never reassigns an order whose rider is already past `to_store`; reassigned orders keep their original queue position.
- Order-service records the assigned `rider_id` and ignores `rider.picked_up/delivered` from any other rider. Every consumer rejects an event that arrives on a topic other than its own (`order.cancelled` on `dispatch.events` goes to the DLQ).
- Search counts only *idle* riders, expands one ring past the first hit, and ranks with each rider's own speed. Matching stops after 5 consecutive misses (fleet exhausted).

## Consequences
- Dynamic key names inside Lua need a single Redis node (not Redis Cluster).
- **Batching potential** is implemented in the scorer and unit-tested, but riders are single-order in P2, so the term is always 0 in practice.
- A stale `rider.picked_up` from a rider benched at the same instant can race the timeout; dispatch ignores it, but order-service may already have advanced. Narrow window, documented.
- Redis is not transactional with Postgres: correctness relies on idempotent claim/release plus the DB being the source of truth for assignment status.
