"""Background loops must never die from one bad iteration."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("darkstore.loop")


async def run_loop(stop: asyncio.Event, step: Callable[[], Awaitable[object]], interval: float,
                   name: str) -> None:
    interval = max(interval, 0.05)
    while not stop.is_set():
        try:
            await step()
        except Exception:  # noqa: BLE001
            log.exception("%s iteration failed; continuing", name)
        await asyncio.sleep(interval)
