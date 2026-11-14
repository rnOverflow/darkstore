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

from . import telemetry
from .db import ServiceMeta
from .envelope import SUPPORTED_SCHEMA_VERSION, Envelope
from .events import PAYLOADS
from .topics import EVENT_TOPICS
from .topics import dlq_topic, parse_delay, retry_topic

log = logging.getLogger("darkstore.consumer")
LAG_EVERY = 5.0  # seconds between lag samples
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
            enable_auto_commit=False, auto_offset_reset="earliest", max_poll_interval_ms=900_000,
            session_timeout_ms=30_000, heartbeat_interval_ms=3_000)
        await consumer.start()
        self._preinit_counters()
        paused: dict[TopicPartition, float] = {}  # partition -> resume time (retry backoff)
        next_lag = 0.0
        try:
            while not stop.is_set():
                if time.time() >= next_lag:
                    next_lag = time.time() + LAG_EVERY
                    await self._report_lag(consumer)
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
                            # The consumer's fetch position is already past the rest of this batch. Rewind
                            # to the first uncommitted message or those are skipped for good (found by the
                            # P6 chaos run: 10 consecutive events never reached a consumer group).
                            try:
                                consumer.seek(tp, m.offset)
                            except IllegalStateError:
                                pass  # partition was revoked: the new owner starts from the committed offset
                            break
        finally:
            telemetry.clear_lag(self.group)
            await consumer.stop()

    def _preinit_counters(self) -> None:
        """Create the retry/dlq series at 0 so the FIRST failure is visible to increase()/rate():
        a series that first appears at 1 has no earlier sample and counts as 0 increase."""
        ev = telemetry.instruments().events
        homes = {t.removeprefix("darkstore.") for t in self.topics}
        for event_type, topic in EVENT_TOPICS.items():
            if topic.value in homes:
                for outcome in ("retry", "dlq"):
                    ev.add(0, {"group": self.group, "event_type": event_type, "outcome": outcome})
        ev.add(0, {"group": self.group, "event_type": "unparseable", "outcome": "dlq"})

    async def _report_lag(self, consumer: AIOKafkaConsumer) -> None:
        """Per group+origin topic: messages not yet processed (log end - position), retry topics
        included, so a backlog parked in a retry topic is visible too."""
        lag: dict[str, int] = {t: 0 for t in self.topics}   # a topic we lost in a rebalance reads 0
        try:
            for tp in consumer.assignment():
                end = consumer.highwater(tp)
                if end is None:
                    continue
                origin = self._retry_info.get(tp.topic, (tp.topic, 0))[0]
                lag[origin] = lag.get(origin, 0) + max(0, end - await consumer.position(tp))
        except Exception:  # noqa: BLE001 - a metrics hiccup (rebalance) must never stop consuming
            return
        telemetry.clear_lag(self.group)
        for origin, v in lag.items():
            telemetry.set_lag(self.group, origin, v)

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
            telemetry.instruments().events.add(
                1, {"group": self.group, "event_type": "unparseable", "outcome": "dlq"})
            return
        ins = telemetry.instruments()
        attrs = {"group": self.group, "event_type": env.event_type}
        if attempt == 0:   # retries re-deliver an old event: their "age" is backoff, not pipeline latency
            ins.event_age.record(max(0.0, time.time() - env.occurred_at.timestamp()), attrs)
        started = time.perf_counter()
        # counters are bumped only after the follow-up produce succeeded: if it raises, run() redoes
        # the message and this method runs again, which must not count it twice
        with telemetry.consume_span(env, self.group, origin, attempt, m.headers) as span:
            try:
                applied = await self._process(env)
                outcome = "ok" if applied else "duplicate"
            except PERMANENT as e:
                log.warning("permanent failure for %s %s: %s", env.event_type, env.event_id, _describe(e))
                telemetry.mark_error(span, _describe(e))
                await self._dlq(m, origin, attempt, f"permanent: {_describe(e)}")
                outcome = "dlq"
            except Exception as e:  # noqa: BLE001 - any other handler failure is retried
                log.warning("handler failed for %s %s: %s", env.event_type, env.event_id, _describe(e))
                telemetry.mark_error(span, _describe(e))
                await self._fail(m, origin, attempt, e)
                outcome = "retry" if attempt < len(self.delays) else "dlq"
            finally:
                ins.event_handle.record(time.perf_counter() - started, attrs)
        ins.events.add(1, {**attrs, "outcome": outcome})

    async def _process(self, env: Envelope) -> bool:
        """True if the handler ran, False for a duplicate delivery."""
        if not self.dedupe:
            await self.handler(None, env)
            return True
        pe = self.meta.processed
        async with self.sm.begin() as s:
            r = await s.execute(pg_insert(pe).values(consumer=self.group, event_id=env.event_id)
                                .on_conflict_do_nothing().returning(pe.c.event_id))
            if r.first() is None:
                return False  # duplicate delivery: already applied
            await self.handler(s, env)
        return True

    async def _fail(self, m: ConsumerRecord, origin: str, attempt: int, err: Exception) -> None:
        if attempt < len(self.delays):
            delay = self.delays[attempt]
            await self.producer.send_and_wait(
                retry_topic(origin, self.group, delay), value=m.value, key=m.key, headers=[
                    ("not_before", str(time.time() + parse_delay(delay)).encode()),
                    ("last_error", _describe(err)[:200].encode()),
                    *telemetry.carry_headers(m.headers)])
        else:
            await self._dlq(m, origin, attempt, _describe(err))

    async def _dlq(self, m: ConsumerRecord, origin: str, attempt: int, error: str) -> None:
        await self.producer.send_and_wait(
            dlq_topic(origin, self.group), value=m.value, key=m.key, headers=[
                ("error", error[:1000].encode()), ("origin_topic", origin.encode()),
                ("attempts", str(attempt).encode()), ("group", self.group.encode()),
                *telemetry.carry_headers(m.headers)])
