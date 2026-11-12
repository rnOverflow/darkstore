# ADR-0003: Idempotency, out-of-order handling, saga
Status: accepted (P1)
## Decision
- **Producer:** transactional outbox in the service's own DB; relay publishes with `acks=all` + idempotent producer, at-least-once.
- **Consumer:** inside ONE DB transaction insert `(consumer, event_id)` into `processed_events`; if the row already exists the event is skipped, otherwise the handler runs. Offsets are committed after the transaction commits.
- **Out-of-order:** order state machine knows each event's required predecessor. Event arriving too early raises `OutOfOrder` -> retry topic with backoff -> DLQ after the last delay. Stale events (already passed that state, or order terminal) are no-ops.
- **Saga:** order-service is the state owner. Inventory reservation is compensated by `order.cancelled` (user cancel, `inventory.rejected` is terminal without compensation, or timeout sweeper). Inventory releases stock on `order.cancelled`; release is idempotent.
- **Retry:** delays 5s, 30s, 5m (configurable); retry consumer sleeps until `not_before` header. DLQ carries `error` and `origin_topic`; `scripts/replay.py` republishes DLQ messages to the origin topic.
## Amendments after P1 review
- **Retry topics break per-order ordering** (a retried `order.created` can run after `order.cancelled`). Compensation is therefore order-independent: inventory and picking write a **tombstone row** when a cancel arrives first, so a late `created`/`reserved` finds an existing row and does nothing.
- **Backoff pauses only the retry partition** (`consumer.pause` + seek back), never the whole consumer; shutdown is prompt.
- **Deterministic failures** (`KeyError`, validation, `ValueError`, `Permanent`) go straight to the DLQ; only transient errors use retries.
- **No header is trusted for routing.** Origin topic and attempt are derived from the topic name; `not_before` is clamped. Replay refuses an `origin_topic` that does not match the DLQ's own name.
- Events with `schema_version` above the consumer's supported version, or an unknown type / invalid payload, go to the DLQ.

## Consequences
Exactly-once *effects*, at-least-once delivery. Sleeping on retry topics blocks that partition; acceptable at this scale, noted for real-scale changes.
