"""The chaos checkers must actually catch what they claim to catch."""
from __future__ import annotations

from collections import Counter

from .invariants import Evidence, WsResult, double_assignments, violations


def good() -> Evidence:
    return Evidence(
        attempts={"k1": {"o1"}, "k2": {"o2"}}, orders={"o1": "delivered", "o2": "delivered"},
        order_rows_for_run=2, delivered_events=Counter(o1=1, o2=1), created_events=Counter(o1=1, o2=1),
        assignment_log=[("o1", "r1", "assigned"), ("o1", "r1", "picked_up"), ("o1", "r1", "delivered"),
                        ("o2", "r1", "assigned"), ("o2", "r1", "delivered")],
        reservations={"o1": "reserved", "o2": "reserved"}, stock_before=100, stock_after=100,
        dlq_before=3, dlq_after=3, ws={"o1": WsResult(["created", "picked", "delivered"], 1, True)})


def test_clean_run_has_no_violations():
    assert violations(good()) == []


def _one(mutate) -> list[str]:
    ev = good()
    mutate(ev)
    return violations(ev)


def test_lost_order_is_detected():
    assert any("LOST" in x for x in _one(lambda e: e.orders.pop("o2")))


def test_retry_that_created_two_orders_is_detected():
    assert any("DUPLICATE" in x for x in _one(lambda e: e.attempts.update(k1={"o1", "o9"})))


def test_extra_order_row_is_detected():
    assert any("DUPLICATE" in x for x in _one(lambda e: setattr(e, "order_rows_for_run", 3)))


def test_stuck_and_undelivered_orders_are_detected():
    assert any("STUCK" in x for x in _one(lambda e: e.orders.update(o2="picked")))
    assert any("NOT DELIVERED" in x for x in _one(lambda e: e.orders.update(o2="cancelled")))
    ev = good()
    ev.orders["o2"], ev.allow_cancel = "cancelled", True
    ev.delivered_events["o2"], ev.reservations["o2"] = 0, "released"
    assert violations(ev) == []


def test_duplicate_delivery_event_is_detected():
    assert any("order.delivered" in x for x in _one(lambda e: e.delivered_events.update(o1=1)))


def test_double_assignment_is_detected():
    log = [("o1", "r1", "assigned"), ("o2", "r1", "assigned")]
    assert double_assignments(log) and double_assignments([("o1", "r1", "assigned"), ("o1", "r2", "assigned")])
    assert double_assignments([("o1", "r1", "delivered")])   # release by a rider who never held it


def test_stock_drift_and_dlq_are_detected():
    assert any("STOCK" in x for x in _one(lambda e: setattr(e, "stock_after", 95)))
    assert any("DLQ" in x for x in _one(lambda e: setattr(e, "dlq_after", 4)))


def test_websocket_gap_and_regression_are_detected():
    bad = WsResult(["created", "picked", "created", "delivered"], 2, True)
    assert any("backwards" in x for x in _one(lambda e: e.ws.update(o1=bad)))
    assert any("never saw" in x for x in _one(lambda e: e.ws.update(o1=WsResult(["created"], 0, False))))
