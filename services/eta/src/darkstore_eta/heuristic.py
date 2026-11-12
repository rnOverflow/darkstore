"""The baseline the model must beat: what an engineer would write without ML.

Distance / speed with a road factor, a flat pick time, and a fixed buffer for P90.
This is also the production fallback whenever the model is off or unusable.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from darkstore_common.geo import DEFAULT_SPEED_KMH, ROAD_FACTOR

PICK_CONST_MIN = 3.0
P90_FACTOR = 1.35
_DEFAULT_KM = 2.0


def _km(v: float) -> float:
    return _DEFAULT_KM if v is None or not math.isfinite(v) else min(60.0, max(0.0, v))


def heuristic(*, stage: int, elapsed_min: float, dist_km: float, rider_to_store_km: float,
              pick_minutes: float | None = None) -> tuple[float, float]:
    """(p50, p90) minutes remaining. Total function: bad inputs are clamped, never raise."""
    stage = min(3, max(0, int(stage if stage is not None and math.isfinite(stage) else 0)))
    ride = _km(dist_km) * ROAD_FACTOR / DEFAULT_SPEED_KMH * 60
    to_store = _km(rider_to_store_km) * ROAD_FACTOR / DEFAULT_SPEED_KMH * 60
    el = elapsed_min if elapsed_min is not None and math.isfinite(elapsed_min) else 0.0
    if stage <= 1:
        p50 = max(ride, PICK_CONST_MIN + to_store + ride - max(0.0, el))
    elif stage == 2:
        p50 = to_store + ride
    else:
        p50 = ride
    return p50, p50 * P90_FACTOR


def heuristic_frame(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised version over a dataset frame (used for evaluation)."""
    ride = df["dist_km"].to_numpy() * ROAD_FACTOR / DEFAULT_SPEED_KMH * 60
    to_store = df["rider_to_store_km"].to_numpy() * ROAD_FACTOR / DEFAULT_SPEED_KMH * 60
    el, st = df["elapsed_min"].to_numpy(), df["stage"].to_numpy()
    early = np.maximum(ride, PICK_CONST_MIN + to_store + ride - np.maximum(0.0, el))
    p50 = np.where(st <= 1, early, np.where(st == 2, to_store + ride, ride))
    return p50, p50 * P90_FACTOR
