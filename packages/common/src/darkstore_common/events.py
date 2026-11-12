"""Typed payloads for every event and a validating factory. See ADR-0002."""
from __future__ import annotations

from pydantic import BaseModel, Field

ID = dict(min_length=1, max_length=64)

from .envelope import Envelope


class Item(BaseModel):
    sku: str = Field(**ID)
    qty: int = Field(gt=0, le=100)


class OrderCreated(BaseModel):
    customer_id: str = Field(**ID)
    store_id: str = Field(**ID)
    items: list[Item] = Field(min_length=1, max_length=50)
    lat: float = Field(default=0.0, ge=-90, le=90)
    lng: float = Field(default=0.0, ge=-180, le=180)


class OrderCancelled(BaseModel):
    reason: str = "customer"


class InventoryReserved(BaseModel):
    store_id: str
    items: list[Item]


class InventoryRejected(BaseModel):
    reason: str
    sku: str | None = None


class PickingStarted(BaseModel):
    store_id: str


class PickingCompleted(BaseModel):
    store_id: str
    pick_seconds: float = 0.0


class RiderAssigned(BaseModel):
    rider_id: str


class Empty(BaseModel):
    pass


PAYLOADS: dict[str, type[BaseModel]] = {
    "order.created": OrderCreated,
    "order.cancelled": OrderCancelled,
    "order.out_for_delivery": Empty,
    "order.delivered": Empty,
    "inventory.reserved": InventoryReserved,
    "inventory.rejected": InventoryRejected,
    "picking.started": PickingStarted,
    "picking.completed": PickingCompleted,
    "rider.assigned": RiderAssigned,
    "rider.picked_up": RiderAssigned,
}


def make_event(event_type: str, order_id: str, **payload) -> Envelope:
    """Validate payload against its model (KeyError for unknown event types)."""
    model = PAYLOADS[event_type]
    clean = model(**payload).model_dump(mode="json")
    return Envelope(event_type=event_type, order_id=order_id, payload=clean)
