"""Replay tool: republish DLQ messages to the topic they came from."""
from __future__ import annotations

import argparse
import asyncio

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient


async def _partitions(bootstrap: str, topic: str) -> list[TopicPartition]:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        info = (await admin.describe_topics([topic]))[0]
        if info.get("error_code"):
            return []  # unknown topic: nothing to replay
        return [TopicPartition(topic, p["partition"]) for p in info["partitions"]]
    finally:
        await admin.close()


async def replay_dlq(bootstrap: str, dlq: str, limit: int | None = None) -> int:
    """Re-send every message currently in `dlq` to its `origin_topic` header; returns the count.

    Messages keep their key and body (so event_id is unchanged and dedupe still applies).
    The DLQ itself is not truncated: it stays an audit log.
    """
    consumer = AIOKafkaConsumer(bootstrap_servers=bootstrap, group_id=None,
                                auto_offset_reset="earliest", enable_auto_commit=False)
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", enable_idempotence=True)
    await consumer.start()
    await producer.start()
    sent = 0
    try:
        parts = await _partitions(bootstrap, dlq)
        if not parts:
            return 0
        consumer.assign(parts)
        await consumer.seek_to_beginning(*parts)
        ends = await consumer.end_offsets(parts)
        while [tp for tp in parts if await consumer.position(tp) < ends[tp]]:
            for msgs in (await consumer.getmany(timeout_ms=500)).values():
                for m in msgs:
                    hdr = dict(m.headers or [])
                    origin = hdr.get("origin_topic", b"").decode(errors="replace")
                    # the DLQ is named <origin>.<group>.dlq: refuse a header pointing anywhere else
                    if not dlq.startswith(origin + ".") or not dlq.endswith(".dlq") or not origin:
                        continue
                    if limit is not None and sent >= limit:
                        continue
                    keep = [(k, v) for k, v in (m.headers or []) if k == "traceparent"]
                    await producer.send_and_wait(origin, value=m.value, key=m.key,
                                                 headers=[*keep, ("replayed", b"1")])
                    sent += 1
    finally:
        await consumer.stop()
        await producer.stop()
    return sent


def cli() -> None:
    ap = argparse.ArgumentParser(description="Replay a DLQ topic back to its origin topic")
    ap.add_argument("dlq", help="e.g. darkstore.order.events.inventory.dlq")
    ap.add_argument("--bootstrap", default="localhost:19092")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    print(f"replayed {asyncio.run(replay_dlq(a.bootstrap, a.dlq, a.limit))} message(s)")
