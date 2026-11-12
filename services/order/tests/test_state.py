import pytest

from darkstore_order.state import OutOfOrder, is_terminal, next_status


def test_happy_path_transitions():
    s = "created"
    for ev, expected in [("inventory.reserved", "reserved"), ("picking.started", "picking"),
                         ("picking.completed", "picked"), ("rider.assigned", "assigned"),
                         ("rider.picked_up", "picked_up"),
                         ("order.out_for_delivery", "out_for_delivery"),
                         ("order.delivered", "delivered")]:
        s = next_status(s, ev)
        assert s == expected


def test_rejection_is_terminal():
    assert next_status("created", "inventory.rejected") == "rejected"
    assert is_terminal("rejected")


def test_event_too_early_raises_out_of_order():
    with pytest.raises(OutOfOrder):
        next_status("reserved", "picking.completed")  # picking.started missing
    with pytest.raises(OutOfOrder):
        next_status("created", "picking.started")


def test_stale_or_duplicate_event_is_noop():
    assert next_status("picking", "inventory.reserved") is None
    assert next_status("picking", "picking.started") is None


def test_terminal_states_ignore_everything():
    for terminal in ("cancelled", "rejected", "delivered"):
        assert next_status(terminal, "inventory.reserved") is None
        assert next_status(terminal, "order.cancelled") is None


def test_cancel_from_any_live_state():
    for live in ("created", "reserved", "picking", "picked", "assigned", "picked_up"):
        assert next_status(live, "order.cancelled") == "cancelled"
