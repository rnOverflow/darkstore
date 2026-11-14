"""The simulated city: Bengaluru-like, six dark stores. Shared by simulator, dispatch, ops."""
from __future__ import annotations

import random

from .geo import haversine_km

CENTER = (12.9716, 77.5946)
HALF_SPAN_DEG = 0.06  # ~6.5 km each way

STORES: dict[str, tuple[float, float]] = {
    "store-0": (12.9352, 77.6245),  # Koramangala
    "store-1": (12.9784, 77.6408),  # Indiranagar
    "store-2": (12.9116, 77.6474),  # HSR Layout
    "store-3": (12.9698, 77.7500),  # Whitefield
    "store-4": (12.9279, 77.5937),  # Jayanagar
    "store-5": (13.0358, 77.5970),  # Hebbal
}


def sim_hour(start_hour: float, speed: float, elapsed_real: float) -> float:
    """Simulated hour-of-day after `elapsed_real` wall seconds at `speed`x."""
    return (start_hour + elapsed_real * speed / 3600.0) % 24


def traffic_factor(hour: float) -> float:
    """Multiplier on rider speed: rush hours are slow, nights are fast."""
    h = hour % 24
    if 8 <= h < 10 or 17 <= h < 21:
        return 0.65
    if h < 6:
        return 1.15
    return 1.0


def nearest_store(lat: float, lng: float) -> str:
    return min(STORES, key=lambda s: haversine_km(lat, lng, *STORES[s]))


def random_point(rng: random.Random) -> tuple[float, float]:
    return (CENTER[0] + rng.uniform(-HALF_SPAN_DEG, HALF_SPAN_DEG),
            CENTER[1] + rng.uniform(-HALF_SPAN_DEG, HALF_SPAN_DEG))


MAX_DELIVERY_KM = 15.0   # farther than any in-city trip; also bounds how long one order can hold a rider


def deliverable(store_id: str, lat: float, lng: float) -> bool:
    """False when the destination is implausibly far from a known store (a guest could otherwise hold a
    rider for hours with an out-of-city destination). Unknown stores pass: inventory rejects them."""
    store = STORES.get(store_id)
    return store is None or haversine_km(*store, lat, lng) <= MAX_DELIVERY_KM
