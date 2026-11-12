"""Turn domain events into what a tracking client sees (pure functions)."""
from __future__ import annotations

from darkstore_common.city import STORES

# forward-only; all three terminal states share the top rank so nothing follows them
RANK = {"created": 0, "reserved": 1, "picking": 2, "picked": 3, "assigned": 4,
        "out_for_delivery": 5, "delivered": 6, "cancelled": 6, "rejected": 6}
TERMINAL = 6

STATUS_OF = {"order.created": "created", "inventory.reserved": "reserved",
             "inventory.rejected": "rejected", "picking.started": "picking",
             "picking.completed": "picked", "rider.assigned": "assigned",
             "order.out_for_delivery": "out_for_delivery", "order.delivered": "delivered",
             "order.cancelled": "cancelled"}


def derive(event_type: str, payload: dict, at: str) -> tuple[str, dict, dict | None] | None:
    """(kind, data, meta) or None if clients do not see this event. meta = static order facts."""
    if event_type in STATUS_OF:
        meta = None
        if event_type == "order.created":
            store = payload.get("store_id")
            slat, slng = STORES.get(store, (None, None))
            meta = {"store": {"id": store, "lat": slat, "lng": slng},
                    "dest": {"lat": payload.get("lat"), "lng": payload.get("lng")}}
        return "status", {"status": STATUS_OF[event_type], "at": at}, meta
    if event_type == "eta.updated":
        return "eta", {"stage": payload["stage"], "p50_real_s": payload["p50_real_s"],
                       "p90_real_s": payload["p90_real_s"], "source": payload["source"], "at": at}, None
    if event_type == "rider.position":
        return "position", {"rider_id": payload["rider_id"], "lat": payload["lat"],
                            "lng": payload["lng"], "at": at}, None
    return None
