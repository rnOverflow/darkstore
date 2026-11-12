"""One simulated clock for the whole system (rider-sim, simulator, ETA all agree on the hour/day).

The epoch is stored in Redis the first time any service asks (SET NX), so every service derives
the same simulated time from the same wall-clock origin.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass(frozen=True)
class SimClock:
    epoch: float
    start_hour: float
    speed: float          # simulated seconds per real second

    def _sim_hours(self, now: float) -> float:
        return self.start_hour + (now - self.epoch) * self.speed / 3600.0

    def hour(self, now: float | None = None) -> float:
        return self._sim_hours(now if now is not None else time.time()) % 24

    def day(self, now: float | None = None) -> int:
        return int(self._sim_hours(now if now is not None else time.time()) // 24)

    def sim_minutes(self, real_seconds: float) -> float:
        return real_seconds * self.speed / 60.0

    def real_seconds(self, sim_minutes: float) -> float:
        return sim_minutes * 60.0 / self.speed


async def load_clock(redis: Redis, start_hour: float, speed: float, prefix: str = "ds:") -> SimClock:
    await redis.set(f"{prefix}sim_epoch", repr(time.time()), nx=True)
    return SimClock(float(await redis.get(f"{prefix}sim_epoch")), start_hour, speed)
