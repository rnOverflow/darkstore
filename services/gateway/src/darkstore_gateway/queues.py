"""Per-connection outbound queue: drop-oldest for positions/ETAs, never drop status.

Messages leave in the order they arrived (ids ascend), so a client that tracks "last applied id"
never skips a status behind a newer position. Only DROPPABLE messages (position, eta: each is
superseded by the next one) are discarded, oldest first, when more than ``max_droppable`` are
waiting. If a client lets even *status* messages pile up past ``max_total`` it is not reading at
all: raise Overflow so the caller closes the connection instead of growing without bound.
"""
from __future__ import annotations

import asyncio
from collections import deque


class Overflow(Exception):
    pass


def stream_id_key(stream_id: str) -> tuple[int, int]:
    """Redis stream ids compare as (ms, seq) integer pairs, never as strings."""
    ms, _, seq = stream_id.partition("-")
    return int(ms), int(seq or 0)


class OutQueue:
    def __init__(self, max_droppable: int = 50, max_total: int = 2000):
        self.max_droppable, self.max_total = max_droppable, max_total
        self._items: deque[tuple[dict, bool]] = deque()
        self._droppable = 0
        self.dropped = 0
        self._wake = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def put(self, msg: dict, droppable: bool) -> None:
        self._items.append((msg, droppable))
        if droppable:
            self._droppable += 1
            while self._droppable > self.max_droppable:
                for i, (_, d) in enumerate(self._items):
                    if d:                       # the oldest droppable message
                        del self._items[i]
                        self._droppable -= 1
                        self.dropped += 1
                        break
        if len(self._items) > self.max_total:
            raise Overflow(f"client is not reading: {len(self._items)} messages queued")
        self._wake.set()

    def pop(self) -> dict | None:
        if not self._items:
            return None
        msg, droppable = self._items.popleft()
        if droppable:
            self._droppable -= 1
        return msg

    def kick(self) -> None:
        """Wake a waiting consumer without queuing anything (used to deliver an Overflow verdict)."""
        self._wake.set()

    async def wait(self, timeout: float | None = None) -> bool:
        """Wait until something is queued (True) or the timeout passes (False)."""
        if self._items:
            return True
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
