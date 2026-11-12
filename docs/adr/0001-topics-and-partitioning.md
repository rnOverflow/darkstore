# ADR-0001: Topics, partition key, naming
Status: accepted (P1)
## Context
Per-order ordering is required (created -> reserved -> picking ...). Kafka orders only within a partition.
## Decision
One topic per producing domain: `<prefix>.order.events`, `.inventory.events`, `.picking.events`, `.dispatch.events`, `.eta.events`, `.rider.positions`. Partition key is always `order_id`, 3 partitions locally. Retry and DLQ topics are **per consumer group**: `<topic>.<group>.retry.<delay>` and `<topic>.<group>.dlq`, so one group's failure never re-delivers to another group.
## Consequences
Ordering holds only within one topic; cross-topic ordering is NOT guaranteed (e.g. `order.cancelled` vs `inventory.reserved`). Consumers must tolerate that via the state machine (ADR-0003).
