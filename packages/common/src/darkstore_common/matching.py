"""Rider scoring. Lower score = better match.

score = eta_to_store_min
      + w_rating * (5 - rating)                 # each rating point costs w_rating minutes
      - w_idle   * min(idle_minutes, cap)       # fairness: reward riders waiting longest (capped)
      - w_batch  * batch_potential              # 0..1: rider already heading to this store
"""
from __future__ import annotations

from dataclasses import dataclass

from .geo import DEFAULT_SPEED_KMH, eta_minutes


@dataclass(frozen=True)
class Weights:
    eta: float = 1.0
    rating: float = 2.0
    idle: float = 0.1
    idle_cap_minutes: float = 30.0
    batch: float = 3.0


@dataclass(frozen=True)
class Candidate:
    rider_id: str
    lat: float
    lng: float
    rating: float = 4.5
    idle_seconds: float = 0.0
    batch_potential: float = 0.0
    speed_kmh: float = DEFAULT_SPEED_KMH


def score(eta_min: float, c: Candidate, w: Weights = Weights()) -> float:
    idle_min = min(c.idle_seconds / 60.0, w.idle_cap_minutes)
    return (w.eta * eta_min + w.rating * (5.0 - c.rating)
            - w.idle * idle_min - w.batch * c.batch_potential)


def rank(candidates: list[Candidate], store_lat: float, store_lng: float,
         w: Weights = Weights()) -> list[tuple[float, float, Candidate]]:
    """Return (score, eta_min, candidate) best-first; ties broken by rider_id."""
    scored = []
    for c in candidates:
        eta = eta_minutes(c.lat, c.lng, store_lat, store_lng, c.speed_kmh)
        scored.append((round(score(eta, c, w), 9), eta, c))
    scored.sort(key=lambda t: (t[0], t[2].rider_id))
    return scored
