"""Geo helpers: haversine, road-factor ETA, H3 cells. Res 8 for matching, res 9 for heatmaps."""
from __future__ import annotations

import math

import h3

MATCH_RES = 8   # ~0.74 km^2 cells: rider search
HEAT_RES = 9    # ~0.1 km^2 cells: supply/demand heatmap
ROAD_FACTOR = 1.4  # straight line -> typical road distance
DEFAULT_SPEED_KMH = 20.0
EARTH_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def eta_minutes(lat1: float, lng1: float, lat2: float, lng2: float,
                speed_kmh: float = DEFAULT_SPEED_KMH) -> float:
    return haversine_km(lat1, lng1, lat2, lng2) * ROAD_FACTOR / speed_kmh * 60


def step_towards(lat: float, lng: float, tlat: float, tlng: float,
                 km: float) -> tuple[float, float, bool]:
    """Move `km` along the straight line to the target; (lat, lng, arrived)."""
    d = haversine_km(lat, lng, tlat, tlng)
    if d <= km or d == 0:
        return tlat, tlng, True
    f = km / d
    return lat + (tlat - lat) * f, lng + (tlng - lng) * f, False


def cell_of(lat: float, lng: float, res: int = MATCH_RES) -> str:
    return h3.latlng_to_cell(lat, lng, res)


def ring(cell: str, k: int) -> list[str]:
    """Cells at grid distance exactly k (k=0 -> the cell itself), sorted for determinism."""
    return [cell] if k == 0 else sorted(h3.grid_ring(cell, k))
