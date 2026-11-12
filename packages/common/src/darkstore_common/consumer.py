"""Idempotent consumer with per-group retry topics and DLQ. See ADR-0003.

Trust model: nothing a producer can put in a Kafka header influences routing. The origin topic
and attempt number are derived from the *topic name* a message arrived on; the only header read
is ``not_before`` (retry topics only), parsed defensively and clamped to the longest delay.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRecord, TopicPartition
from aiokafka.errors import CommitFailedError, IllegalStateError
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import ServiceMeta
from .envelope import SUPPORTED_SCHEMA_VERSION, Envelope
from .events import PAYLOADS
from .topics import EVENT_TOPICS
from .topics import dlq_topic, parse_delay, retry_topic

log = logging.getLogger("darkstore.consumer")
Handler = Callable[[AsyncSession, Envelope], Awaitable[None]]


class Permanent(Exception):
    """Raise from a handler for failures that retrying cannot fix (goes straight to the DLQ)."""


# Deterministic failures: retrying wastes minutes and blocks the partition.
PERMANENT = (Permanent, KeyError, ValidationError, ValueError, TypeError)


def _describe(e: Exception) -> str:
    # DB errors embed bound parameters (customer ids, items): keep only the class name.
    if isinstance(e, DBAPIError):
        return type(e).__name__
    return f"{type(e).__name__}: {e}"[:300]


class ConsumerRunner:
    def __init__(self, *, bootstrap: str, group: str, topics: list[str],
                 sessionmaker: async_sessionmaker[AsyncSession], meta: ServiceMeta,
                 handler: Handler, producer: AIOKafkaProducer,
                 retry_delays: tuple[str, ...] = ("5s", "30s", "5m"), dedupe: bool = True):
        self.bootstrap, self.group, self.topics = bootstrap, group, topics
        self.sm, self.meta, self.handler, self.producer = sessionmaker, meta, handler, producer
        self.delays = retry_delays
        self.dedupe = dedupe       # False: the handler is idempotent itself and needs no DB (gateway)
        self.max_delay = max(parse_delay(d) for d in retry_delays) if retry_delays else 0.0
        # retry topic name -> (origin topic, attempts already made)
        self._retry_info = {retry_topic(t, group, d): (t, i + 1)
                            for t in topics for i, d in enumerate(retry_delays)}

    def owned_topics(self) -> list[str]:
        """Retry + DLQ topics this runner needs (the service creates them at startup)."""
        return [*self._retry_info, *[dlq_topic(t, self.group) for t in self.topics]]

    async def run(self, stop: asyncio.Event) -> None:
        consumer = AIOKafkaConsumer(
            *self.topics, *self._retry_info, bootstrap_servers=self.bootstrap, group_id=self.group,
            enable_auto_commit=False, auto_offset_reset="earliest", max_poll_interval_ms=900_000)
        await consumer.start()
        paused: dict[TopicPartition, float] = {}  # partition -> resume time (retry backoff)
        try:
            while not stop.is_set():
                for tp, at in list(paused.items()):
                    if at <= time.time():
                        try:
                            consumer.resume(tp)
                        except IllegalStateError:
                            pass  # partition was revoked in a rebalance: nothing to resume
                        del paused[tp]
                batch = await consumer.getmany(timeout_ms=500, max_records=50)
                for tp, msgs in batch.items():
                    for m in msgs:
                        if stop.is_set():
                            break
                        due = self._not_before(m)
                        if due > time.time():  # back off this partition only; others keep flowing
                            try:
                                consumer.seek(tp, m.offset)
                                consumer.pause(tp)
                                paused[tp] = due
                            except IllegalStateError:
                                pass  # lost the partition in a rebalance; new owner will redeliver
                            break
                        try:
                            await self._handle(m)
                        except Exception:  # noqa: BLE001 - e.g. Kafka produce failed: redo message
                            log.exception("failed handling %s[%s]@%s; will redo", m.topic,
                                          m.partition, m.offset)
                            try:
                                consumer.seek(tp, m.offset)
                            except IllegalStateError:
                                pass
                            await asyncio.sleep(1)
                            break
                        try:
                            await consumer.commit({tp: m.offset + 1})  # after the DB tx committed
                        except (CommitFailedError, IllegalStateError):
                            # rebalance took the partition: the new owner redelivers, and
                            # processed_events makes the repeat harmless
                            log.warning("commit failed (rebalance); message will be redelivered")
                            break
        finally:
            await consumer.stop()

    def _not_before(self, m: ConsumerRecord) -> float:
        if m.topic not in self._retry_info:
            return 0.0
        try:
            raw = dict(m.headers or []).get("not_before", b"0").decode()
            return min(float(raw), time.time() + self.max_delay)
        except (ValueError, UnicodeDecodeError):
            return 0.0

    async def _handle(self, m: ConsumerRecord) -> None:
        origin, attempt = self._retry_info.get(m.topic, (m.topic, 0))
        try:
            env = Envelope.model_validate_json(m.value)
            PAYLOADS[env.event_type].model_validate(env.payload)
            home = EVENT_TOPICS[env.event_type].value
            if not (origin == home or origin.endswith("." + home)):
                # a forged / misrouted event: e.g. order.cancelled published to dispatch.events
                raise ValueError(f"{env.event_type} does not belong on {origin}")
            if env.schema_version > SUPPORTED_SCHEMA_VERSION:
                raise ValueError(f"unsupported schema_version {env.schema_version}")
        except (ValidationError, KeyError, ValueError) as e:
            await self._dlq(m, origin, attempt, f"poison message: {type(e).__name__}")
            return
        try:
            await self._process(env)
        except PERMANENT as e:
            log.warning("permanent failure for %s %s: %r", env.event_type, env.event_id, e)
            await self._dlq(m, origin, attempt, f"permanent: {_describe(e)}")
        except Exception as e:  # noqa: BLE001 - any other handler failure is retried
            log.warning("handler failed for %s %s: %s", env.event_type, env.event_id, _describe(e))
            await self._fail(m, origin, attempt, e)

    async def _process(self, env: Envelope) -> None:
        if not self.dedupe:
            await self.handler(None, env)
            return
        pe = self.meta.processed
        async with self.sm.begin() as s:
            r = await s.execute(pg_insert(pe).values(consumer=self.group, event_id=env.event_id)
                                .on_conflict_do_nothing().returning(pe.c.event_id))
            if r.first() is None:
                return  # duplicate delivery: already applied
            await self.handler(s, env)

    async def _fail(self, m: ConsumerRecord, origin: str, attempt: int, err: Exception) -> None:
        if attempt < len(self.delays):
            delay = self.delays[attempt]
            await self.producer.send_and_wait(
                retry_topic(origin, self.group, delay), value=m.value, key=m.key, headers=[
                    ("not_before", str(time.time() + parse_delay(delay)).encode()),
                    ("last_error", _describe(err)[:200].encode())])
        else:
            await self._dlq(m, origin, attempt, _describe(err))

    async def _dlq(self, m: ConsumerRecord, origin: str, attempt: int, error: str) -> None:
        await self.producer.send_and_wait(
            dlq_topic(origin, self.group), value=m.value, key=m.key, headers=[
                ("error", error[:1000].encode()), ("origin_topic", origin.encode()),
                ("attempts", str(attempt).encode()), ("group", self.group.encode())])
