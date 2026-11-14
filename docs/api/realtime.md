# Realtime + ops API contract (P4)

Everything is same-origin behind nginx at **http://localhost:8088**:

| Path | Goes to |
|---|---|
| `/` | customer UI (static) |
| `/ops/` | ops UI (static) |
| `/ws` | tracking gateway (WebSocket, 2 replicas) |
| `/api/orders...` | order-service (REST, existing) |
| `/api/city` | ops-api, public |
| `/api/ops/...` | ops-api, needs `Authorization: Bearer <ops token>` |

## Order REST (authentication: ADR-0007)
1. `POST /api/sessions` (anonymous, 5 r/s per IP) returns `201 {"customer_id": "g-...", "token": "<guest token>"}`.
2. `POST /api/orders` with `Authorization: Bearer <guest token>` and an `Idempotency-Key` header (1-64 chars of
   `A-Za-z0-9._:-`). Body `{store_id, items:[{sku,qty}], lat, lng}`; a `customer_id` in the body is ignored (identity
   comes from the token). Returns `201 {"order_id", "status": "created", "tracking_token"}`; an idempotent replay
   returns `200` with the same order and a fresh tracking token (keys are scoped to the session, so a replay is by the owner; a lost 201 does not strand the order). `409` if the same key is reused with a different body; `422` if the destination is more than 15 km from the store.
3. `GET /api/orders/{id}` and `POST /api/orders/{id}/cancel` need `Bearer <tracking token for that order>` (or ops).
   Another order's token gets 403; missing/invalid/expired gets 401. Bad id and bad token are indistinguishable.
Stores/city: `GET /api/city`:
```json
{"center": [12.9716, 77.5946], "bounds": [[12.9116,77.5346],[13.0316,77.6546]],
 "stores": {"store-0": [12.9352, 77.6245], "...": []}, "sim_speed": 30}
```
(coordinates are `[lat, lng]`; MapLibre wants `[lng, lat]`, so swap when drawing.)
Order items: skus are `sku-0` .. `sku-19`, qty 1-3, 1-4 items.

## WebSocket `/ws` (JSON text frames)
Client -> server:
1. `{"type":"auth","token":"<tracking or ops token>"}` must be the FIRST frame within 5 s.
2. `{"type":"subscribe","order_id":"<id>","last_event_id":"<id or null>"}` (customer tokens may only
   subscribe to the order they were issued for).
3. `{"type":"pong"}` reply to heartbeats.
4. `{"type":"ack"}` **flow control**: send at most once per second while receiving frames. The server
   pauses sending after `ws_ack_window` (500) unacknowledged messages and closes a client that
   stays silent for `ws_stall_timeout` (30 s) while paused. `pong` also counts as an ack.

Server -> client:
- `{"type":"auth_ok","role":"customer"|"ops"}`
- `{"type":"snapshot", "order_id", "last_event_id", "status", "store":{"id","lat","lng"} | null,
   "dest":{"lat","lng"} | null, "eta": EtaData | null, "rider": {"rider_id","lat","lng"} | null,
   "history":[{"status","at"}]}` sent immediately after subscribe.
- `{"type":"event","id":"<stream id>","kind":"status"|"eta"|"position"|"meta","data":{...}}`
  (`meta` is additive; a client that does not know a kind ignores the frame)
- `{"type":"heartbeat","ts":<unix seconds>}` every 15 s
- `{"type":"error","code":"unauthorized"|"forbidden"|"bad_request"|"timeout","message":"..."}` then
  the socket closes with code 4401 (unauthorized) / 4403 (forbidden) / 4400 / 4408.

Event data:
- status: `{"status": Status, "at": "<iso>"}`. Status is one of
  `created, reserved, picking, picked, assigned, out_for_delivery, delivered, cancelled, rejected`
  (forward-only; terminal: delivered, cancelled, rejected).
- eta: `EtaData = {"stage": "created|picking|assigned|out_for_delivery", "p50_real_s": number,
  "p90_real_s": number, "source": "model"|"heuristic", "at": "<iso>"}`. Seconds are wall-clock
  seconds REMAINING at time `at`; the UI counts down from there.
- position: `{"rider_id": str, "lat": number, "lng": number, "at": "<iso>"}`, about 1/s while the
  rider is busy.
- meta: `{"store": {"id","lat","lng"}, "dest": {"lat","lng"}}`, sent once per order, as soon as the
  gateway has seen `order.created`. The same facts are in every later snapshot. It exists because a
  client can subscribe (and receive a snapshot with `store`/`dest` null) before the gateway has
  consumed `order.created`, and because that event can arrive after a later status. It is never
  dropped under backpressure (unlike `eta` and `position`).

### Resume / ordering rules (client MUST follow)
- Event ids are Redis stream ids (`"1731000000000-0"`): compare as (ms, seq) integer pairs.
- On connect the client sends `subscribe` with the last `id` it applied (or null). The server sends a
  **snapshot first**, then replays any events newer than `last_event_id` that the snapshot does not
  already contain, then live events. The client ignores any event whose id <= the last applied id.
- Reconnect with exponential backoff (500 ms doubling to 10 s, jitter) and re-auth + re-subscribe.
- Backpressure: while the server is paused waiting for acks it keeps a bounded queue per client and
  drops the OLDEST `position`/`eta` events (superseded by newer ones) but NEVER a `status` event.
  Resume is gap-free by *state*: every subscribe returns a fresh snapshot (its `history` lists every
  status reached) and `last_event_id` only tells the server where the client was; events newer than
  the snapshot follow live.

## Ops REST (Bearer ops token; JSON)
- `GET /api/ops/summary` ->
  `{"funnel": {"created": n, "reserved": n, "picking": n, "picked": n, "assigned": n,
   "out_for_delivery": n, "delivered": n, "cancelled": n, "rejected": n},
   "pick_p95_sim_min": number | null, "pick_samples": n, "orders_last_5m": n,
   "riders": {"idle": n, "assigned": n, "cooldown": n}, "sim_speed": 30, "generated_at": "<iso>"}`
- `GET /api/ops/riders` -> `{"riders":[{"id","lat","lng","status","order_id"|null}]}` (all riders)
- `GET /api/ops/heatmap` -> `{"cells":[{"cell": "<h3 res9>", "supply": n, "demand": n,
   "boundary": [[lat,lng], ...]}]}` supply = idle riders in the cell, demand = orders created in the
   last 10 real minutes into the cell.
- `GET /api/ops/late` -> `{"late":[{"order_id","status","store_id","elapsed_sim_min",
   "promised_p90_sim_min","over_by_sim_min"}]}` active orders already past their first P90, worst first, max 20.

## Tokens
Opaque to the UI. The customer UI gets `tracking_token` from the order response. The ops UI reads the
ops token from the URL fragment (`/ops/#token=...`) and sends it as the Bearer header for REST and as
the `auth` frame for WS. If a token is rejected (HTTP 401 / WS 4401) show "session expired", do not loop.
