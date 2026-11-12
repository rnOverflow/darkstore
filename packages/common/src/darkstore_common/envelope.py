"""Event envelope shared by every Kafka message.

Partition key is always ``order_id`` so all events for one order are ordered.
``event_id`` is the idempotency key used by consumers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Envelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    schema_version: int = 1
    occurred_at: datetime = Field(default_factory=_now)
    order_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    def partition_key(self) -> bytes:
        return self.order_id.encode()
