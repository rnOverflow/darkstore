"""Demand curve with rush-hour spikes: orders per (simulated) minute for a given hour of day."""
from __future__ import annotations

# (start_hour, end_hour, multiplier on the base rate)
_BANDS = [(0, 6, 0.2), (6, 8, 0.6), (8, 10, 1.8), (10, 12, 1.0), (12, 14, 2.2),
          (14, 17, 0.7), (17, 19, 1.2), (19, 22, 3.0), (22, 24, 0.8)]


def orders_per_minute(hour: float, base: float) -> float:
    h = hour % 24
    for lo, hi, mult in _BANDS:
        if lo <= h < hi:
            return base * mult
    return base
