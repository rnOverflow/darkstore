"""Topic registry. Detailed rationale lives in docs/adr (written in P1)."""
from __future__ import annotations

from enum import Enum


class Topic(str, Enum):
    ORDER_EVENTS = "darkstore.order.events"
    INVENTORY_EVENTS = "darkstore.inventory.events"
    PICKING_EVENTS = "darkstore.picking.events"
    DISPATCH_EVENTS = "darkstore.dispatch.events"
    ETA_EVENTS = "darkstore.eta.events"
    RIDER_POSITIONS = "darkstore.rider.positions"


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


def retry_topic(topic: Topic, delay: str) -> str:
    return f"{topic.value}.retry.{delay}"


def dlq_topic(topic: Topic) -> str:
    return f"{topic.value}.dlq"
