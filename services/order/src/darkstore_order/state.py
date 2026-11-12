"""Order state machine. See ADR-0003: too-early events raise OutOfOrder (-> retry), stale ones are no-ops."""
from __future__ import annotations


class OutOfOrder(Exception):
    """Event arrived before its predecessor; retry later."""


RANK = ["created", "reserved", "picking", "picked", "assigned", "picked_up",
        "out_for_delivery", "delivered"]
TERMINAL = {"rejected", "cancelled", "delivered"}

# event -> (required current status, resulting status)
EVENTS = {
    "inventory.reserved": ("created", "reserved"),
    "inventory.rejected": ("created", "rejected"),
    "picking.started": ("reserved", "picking"),
    "picking.completed": ("picking", "picked"),
    "rider.assigned": ("picked", "assigned"),
    "rider.picked_up": ("assigned", "picked_up"),
    "rider.delivered": ("out_for_delivery", "delivered"),
    "order.out_for_delivery": ("picked_up", "out_for_delivery"),
    "order.delivered": ("out_for_delivery", "delivered"),
}


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def next_status(current: str, event_type: str) -> str | None:
    """Return the new status, None if the event is stale/no-op, raise OutOfOrder if too early."""
    if is_terminal(current):
        return None
    if event_type == "order.cancelled":
        return "cancelled"
    required, result = EVENTS[event_type]
    if current == required:
        return result
    if RANK.index(current) > RANK.index(required):
        return None
    raise OutOfOrder(f"{event_type} needs status {required!r}, order is {current!r}")
