"""Topic registry. See docs/adr/0001-topics-and-partitioning.md."""
from __future__ import annotations

from enum import Enum


class Topic(str, Enum):
    ORDER_EVENTS = "order.events"
    INVENTORY_EVENTS = "inventory.events"
    PICKING_EVENTS = "picking.events"
    DISPATCH_EVENTS = "dispatch.events"
    ETA_EVENTS = "eta.events"
    RIDER_POSITIONS = "rider.positions"


EVENT_TOPICS: dict[str, Topic] = {
    "order.created": Topic.ORDER_EVENTS,
    "order.out_for_delivery": Topic.ORDER_EVENTS,
    "order.delivered": Topic.ORDER_EVENTS,
    "order.cancelled": Topic.ORDER_EVENTS,
    "inventory.reserved": Topic.INVENTORY_EVENTS,
    "inventory.rejected": Topic.INVENTORY_EVENTS,
    "picking.started": Topic.PICKING_EVENTS,
    "picking.completed": Topic.PICKING_EVENTS,
    "rider.assigned": Topic.DISPATCH_EVENTS,
    "rider.picked_up": Topic.DISPATCH_EVENTS,
}


def retry_topic(origin: str, group: str, delay: str) -> str:
    return f"{origin}.{group}.retry.{delay}"


def dlq_topic(origin: str, group: str) -> str:
    return f"{origin}.{group}.dlq"


def parse_delay(delay: str) -> float:
    """'5s' -> 5.0, '5m' -> 300.0, '0.5s' -> 0.5."""
    try:
        if delay.endswith("s"):
            return float(delay[:-1])
        if delay.endswith("m"):
            return float(delay[:-1]) * 60
    except ValueError:
        pass
    raise ValueError(f"bad delay {delay!r}; use e.g. '5s' or '5m'")
