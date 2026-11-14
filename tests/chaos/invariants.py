"""Pure invariant checks for the chaos runs: given evidence read from the system, list violations.

Kept free of I/O so the checks themselves are unit-tested (tests/chaos/test_invariants.py): a checker
that cannot fail proves nothing.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

RANK = ["created", "reserved", "picking", "picked", "assigned", "picked_up", "out_for_delivery", "delivered"]
TERMINAL = {"delivered", "cancelled", "rejected"}


@dataclass
class WsResult:
    statuses: list[str] = field(default_factory=list)   # every status frame/snapshot the client applied
    reconnects: int = 0
    reached_delivered: bool = False


@dataclass
class Evidence:
    attempts: dict[str, set[str]]           # idempotency key -> order ids the API returned over all attempts
    orders: dict[str, str]                  # order_id -> status, for this run's orders
    order_rows_for_run: int                 # rows in orders.orders for this run's customer
    delivered_events: Counter               # order_id -> order.delivered outbox rows
    created_events: Counter                 # order_id -> order.created outbox rows
    assignment_log: list[tuple[str, str, str]]   # (order_id, rider_id, action), run's orders, in order
    reservations: dict[str, str]            # order_id -> reserved | released | rejected
    stock_before: int                       # available + reserved qty before the run
    stock_after: int
    dlq_before: int
    dlq_after: int
    ws: dict[str, WsResult] = field(default_factory=dict)
    allow_cancel: bool = False


def double_assignments(log: list[tuple[str, str, str]]) -> list[str]:
    """Replay the append-only assignment log: a rider never holds two orders, an order never two riders."""
    out, by_rider, by_order = [], {}, {}
    for oid, rid, action in log:
        if action == "assigned":
            if rid in by_rider:
                out.append(f"rider {rid} assigned {oid} while holding {by_rider[rid]}")
            if oid in by_order:
                out.append(f"order {oid} assigned to {rid} while held by {by_order[oid]}")
            by_rider[rid], by_order[oid] = oid, rid
        elif action in ("timed_out", "cancel_release", "delivered"):
            if by_rider.get(rid) != oid:
                out.append(f"{action}: rider {rid} did not hold {oid}")
            by_rider.pop(rid, None)
            by_order.pop(oid, None)
        elif by_rider.get(rid) != oid:   # picked_up
            out.append(f"{action}: rider {rid} did not hold {oid}")
    return out


def violations(ev: Evidence) -> list[str]:
    v: list[str] = []
    for key, ids in ev.attempts.items():
        if len(ids) != 1:
            v.append(f"DUPLICATE: key {key} produced {len(ids)} orders {sorted(ids)}")
    accepted = {oid for ids in ev.attempts.values() for oid in ids}
    for oid in sorted(accepted - ev.orders.keys()):
        v.append(f"LOST: order {oid} was acknowledged to the client but is not in the database")
    if ev.order_rows_for_run != len(ev.attempts):
        v.append(f"DUPLICATE: {ev.order_rows_for_run} order rows for {len(ev.attempts)} distinct requests")
    for oid, status in sorted(ev.orders.items()):
        if status not in TERMINAL:
            v.append(f"STUCK: order {oid} is still '{status}'")
        elif status != "delivered" and not ev.allow_cancel:
            v.append(f"NOT DELIVERED: order {oid} ended '{status}'")
        if ev.created_events[oid] != 1:
            v.append(f"order {oid}: {ev.created_events[oid]} order.created events (want 1)")
        want = 1 if status == "delivered" else 0
        if ev.delivered_events[oid] != want:
            v.append(f"order {oid} ({status}): {ev.delivered_events[oid]} order.delivered events (want {want})")
        res = ev.reservations.get(oid)
        if status == "delivered" and res != "reserved":
            v.append(f"order {oid} delivered but reservation is {res!r}")
        if status == "cancelled" and res not in ("released", None):
            v.append(f"order {oid} cancelled but reservation is {res!r} (stock leak)")
    v += double_assignments(ev.assignment_log)
    if ev.stock_after != ev.stock_before:
        v.append(f"STOCK DRIFT: available+reserved went {ev.stock_before} -> {ev.stock_after}")
    if ev.dlq_after != ev.dlq_before:
        v.append(f"DLQ: {ev.dlq_after - ev.dlq_before} message(s) were dead-lettered during the run")
    for oid, w in sorted(ev.ws.items()):
        if not w.reached_delivered:
            v.append(f"WS client for {oid} never saw 'delivered' (saw {w.statuses[-3:]})")
        ranks = [RANK.index(s) for s in w.statuses if s in RANK]
        if any(b < a for a, b in zip(ranks, ranks[1:])):
            v.append(f"WS client for {oid} saw a status go backwards: {w.statuses}")
    return v
