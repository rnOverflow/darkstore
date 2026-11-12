"""JSON Schemas for every event, registered in the Redpanda Schema Registry (ADR-0002)."""
from __future__ import annotations

import json

import httpx

from .envelope import Envelope
from .events import PAYLOADS
from .topics import EVENT_TOPICS


def event_schema(event_type: str) -> dict:
    """Envelope schema with the payload narrowed to this event's model."""
    schema = Envelope.model_json_schema()
    payload = PAYLOADS[event_type].model_json_schema()
    defs = {**schema.get("$defs", {}), **payload.pop("$defs", {})}
    schema["properties"]["payload"] = payload
    schema["properties"]["event_type"] = {"const": event_type}
    if defs:
        schema["$defs"] = defs
    return schema


def subject(prefix: str, event_type: str) -> str:
    return f"{prefix}.{EVENT_TOPICS[event_type].value}-{event_type}"


def register_all(registry_url: str, prefix: str = "darkstore") -> dict[str, int]:
    """Register (idempotent) and set BACKWARD compatibility. Returns subject -> schema id."""
    ids = {}
    with httpx.Client(base_url=registry_url, timeout=10) as c:
        for ev in EVENT_TOPICS:
            sub = subject(prefix, ev)
            c.put(f"/config/{sub}", json={"compatibility": "BACKWARD"}).raise_for_status()
            r = c.post(f"/subjects/{sub}/versions",
                       json={"schemaType": "JSON", "schema": json.dumps(event_schema(ev))})
            r.raise_for_status()
            ids[sub] = r.json()["id"]
    return ids
