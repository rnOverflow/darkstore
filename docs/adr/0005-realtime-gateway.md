# ADR-0005: Realtime tracking gateway
Status: accepted (P4). Wire protocol: [docs/api/realtime.md](../api/realtime.md).

## Context
Customers watch one order live (status, rider position, ETA) and must survive disconnects, gateway
restarts and slow networks. Gateway replicas must be interchangeable behind a load balancer.

## Decision
- **Fan-out through Redis, one Kafka consumer group.** Replicas share the consumer group
  `gateway` (each Kafka event is handled once). The handler does one Lua script per event that
  dedupes by `event_id`, enforces **forward-only status**, appends to a bounded per-order Redis
  stream, updates the order's state hash and `PUBLISH`es, all atomically. Every replica runs a
  pattern subscriber and forwards publishes to its own sockets, so any replica can serve any order.
- **Snapshot-on-connect makes resume gap-free by state.** Subscribe registers the socket in a
  "syncing" state (live events are buffered), reads the snapshot (which carries `last_event_id` and the
  full status `history`), sends it, then flushes buffered events newer than the snapshot. Because the
  snapshot and the stream are written by the same atomic script, nothing can fall between them.
  `last_event_id` is advisory; the client ignores events with an id <= the last one applied.
- **Backpressure = application-level flow control.** uvicorn's transport buffers outgoing data
  without bound, so a stalled reader is invisible to `send()` (verified: 40 MB sent to a client that
  stopped reading with zero drops). The client therefore acks about once a second; the server pauses
  after 500 unacknowledged messages and disconnects a client that stays silent 30 s. While paused,
  a bounded per-connection queue drops the oldest `position`/`eta` events but never a `status`;
  messages leave in arrival order so a client never skips a status behind a newer position.
- **Auth: signed, expiring, order-bound tokens (HMAC-SHA256).** Order creation returns a
  `tracking_token` valid only for that order; ops tokens (role `ops`) unlock `/api/ops/*`. The first
  WS frame must be `auth` within 5 s. Unknown/expired/forged tokens: close 4401; wrong order: 4403.
- **Ops dashboard uses polled REST (2 s), not WebSocket.** City-level aggregates are cheap to compute
  on demand and need no per-client state; a push channel can replace polling later without API changes.
- **One origin.** nginx serves both UIs and proxies `/ws` (upstream of both replicas, with
  `proxy_next_upstream`) and `/api`, so there is no CORS surface and a dead replica is invisible to a
  reconnecting client.

## Consequences
- Redis is a single point of failure for live tracking (Kafka remains the source of truth, so state
  can be rebuilt by replay). Dynamic multi-key Lua also means single-node Redis (see ADR-0004).
- Dropping positions for slow clients is lossy by design; statuses are the durable signal.
- Tokens are bearer secrets in a URL query for tracking links (like a courier tracking link); they are
  short-lived and order-scoped, but treat them as sensitive.
- Order creation is still unauthenticated (nginx rate-limits it); real customer authn is P7.
