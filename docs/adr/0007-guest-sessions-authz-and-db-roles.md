# ADR-0007: Guest sessions, per-order authorization, least-privilege DB roles, retention

Status: accepted (P7)

## Context
Before P7 the order API trusted a client-supplied `customer_id` and any bearer could read any order. Every service
shared one Postgres superuser, and the outbox / processed_events tables grew without bound.

## Decisions
1. **Anonymous guest sessions.** `POST /sessions` returns a server-assigned `g-…` customer id and a `guest` token that
   can only create orders. There are no real accounts (known deviation: this is a demo platform). Body `customer_id`
   is ignored.
2. **Per-order tracking token.** Creating an order returns a token scoped to that order; per-order reads need it (or
   an `ops` token). Authorization is checked *before* the lookup, so a wrong token gives the same answer whether or
   not the order exists (no existence oracle). Gateway/WS only accept `customer`/`ops` roles; guest tokens are refused.
3. **Docs off by default** (`expose_docs`), the order service refuses to start without `TOKEN_SECRET`.
4. **One Postgres role per service**, each owning only its schema; `ops_api` is read-only on orders/picking/eta.
   Consequence: changing roles needs `make clean` (fresh volume).
5. **Retention janitor** in every service: published outbox rows 1 h, `processed_events` 24 h (must outlive the longest
   redelivery path: 5-minute retry topics, later DLQ replay). Unpublished outbox rows are never deleted.
6. Envelope `order_id`/`event_id` bounded to 64 chars; span error text never includes SQL parameters.
7. nginx rate limits: orders 20 r/s, api 30 r/s, sessions 5 r/s, 20 WS connections per IP.

## Consequences
Guests cannot enumerate or read other customers' orders. A leaked tracking token exposes one order only. The trade-off
is no cross-device history; real accounts (OIDC) would replace step 1 at production scale.

## Known deviations (accepted, not fixed)
- **F2 Shared HMAC secret.** Tokens are signed with one `TOKEN_SECRET`; any service holding it can mint any token, there
  is no `kid`, rotation or revocation. At scale: asymmetric signing so only the order service can mint, `kid` + overlap
  window for rotation, `jti` denylist for revocation.
- **F3 No per-customer active-order cap.** Guest sessions are free (5 r/s at nginx), orders are limited to 20 r/s, but
  an unassigned order holds its stock reservation until it times out, so a determined client can tie up stock. A cap
  per customer would be easy to add; it would also need the load and chaos harnesses to mint one session per N orders.
- **F4 Dev ports.** Postgres, Redis (password only), Prometheus, Grafana, MLflow, Redpanda Console and the Kafka
  listener are published on 127.0.0.1 without per-user auth. Fine for a laptop; not for anything exposed.
- Images are pinned by tag, not digest; Kafka has no SASL/TLS or ACLs; there is no Alertmanager.
- **ops-api read scope.** Its role gets default `SELECT` on every table in `orders`, `picking`, `eta`, including
  `orders.outbox` / `processed_events` (event bodies hold customer id and coordinates). It only needs three tables.
  Per-table grants cannot be set at init because each service creates its tables later; the fix is for each service's
  `init_schema` to grant only its ops-readable tables. The integration tests connect as a superuser, so the role
  grants are exercised only by the running stack, not by tests.
- **WebSocket outlives its token.** The gateway verifies the token once at connect; an open socket keeps working
  after the 6 h token expires. Fix: close the socket at `exp`.
- **Delivery area.** Order creation rejects destinations more than 15 km from a known store (`city.deliverable`);
  before this a guest could hold a rider for hours with an out-of-city destination (found by the final review).
