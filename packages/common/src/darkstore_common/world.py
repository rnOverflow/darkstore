"""The simulated world's physics, shared by the live simulators and the offline trip generator.

Both the running rider-sim and the ETA dataset generator call these functions, so a model trained
offline is trained on the same effects the live system produces: rush-hour traffic, rain, slow
neighbourhoods and picker queues. (Single source of truth; do not duplicate these constants.)
"""
from __future__ import annotations

import hashlib
import math
import random

from .city import traffic_factor

RAIN_PROBABILITY = 0.25
RAIN_SPEED_FACTOR = 0.78
PICKERS_PER_STORE = 2
BASE_PICK_MINUTES = 2.5


def _unit(key: str) -> float:
    """Deterministic pseudo-random float in [0, 1) from a string."""
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0x100000000


def is_raining(sim_day: int) -> bool:
    return _unit(f"rain:{sim_day}") < RAIN_PROBABILITY


def weather_speed_factor(raining: bool) -> float:
    return RAIN_SPEED_FACTOR if raining else 1.0


def cell_speed_factor(cell: str) -> float:
    """Some H3 cells are persistently slower (junctions, markets): factor in [0.7, 1.1]."""
    return 0.7 + 0.4 * _unit(f"cell:{cell}")


def speed_multiplier(hour: float, raining: bool, cell: str) -> float:
    return traffic_factor(hour) * weather_speed_factor(raining) * cell_speed_factor(cell)


def pick_minutes(rng: random.Random, queue_depth: int, raining: bool) -> float:
    """Dark-store pick+pack time: lognormal, slower when pickers are queued and when it rains."""
    mean = BASE_PICK_MINUTES * (1 + 0.35 * queue_depth) * (1.15 if raining else 1.0)
    sigma = 0.35
    return rng.lognormvariate(math.log(mean) - sigma ** 2 / 2, sigma)
