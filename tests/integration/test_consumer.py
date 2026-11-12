import asyncio
"""Idempotent consumer + retry + DLQ semantics against real Kafka/Postgres."""
import pytest

from darkstore_common.db import service_meta
from darkstore_common.events import make_event
from darkstore_common.service import run_service
from darkstore_common.topics import Topic, dlq_topic

from .helpers import publish, read_all, running, until

pytestmark = pytest.mark.integration
GROUP = "ctest"


def _ev(oid="o1"):
    return make_event("order.cancelled", oid, reason="t")


async def test_duplicate_delivery_runs_handler_once(env):
    seen = []

    async def handler(session, e):
        seen.append(e.event_id)

    topic = env.topic(Topic.ORDER_EVENTS)
    e = _ev()
    async with running(lambda stop: run_service(env, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        for _ in range(3):
            await publish(env.kafka_bootstrap, topic, e)
        marker = _ev("o2")
        await publish(env.kafka_bootstrap, topic, marker)
        await until(lambda: _async(marker.event_id in seen))
    assert seen.count(e.event_id) == 1


async def test_failure_is_retried_then_succeeds(env):
    attempts = []

    async def handler(session, e):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")

    topic = env.topic(Topic.ORDER_EVENTS)
    async with running(lambda stop: run_service(env, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        await publish(env.kafka_bootstrap, topic, _ev())
        await until(lambda: _async(len(attempts) >= 3), timeout=40)
    assert len(attempts) == 3
    assert await read_all(env.kafka_bootstrap, dlq_topic(topic, GROUP), wait=2) == []


async def test_exhausted_retries_go_to_dlq_with_error_header(env):
    async def handler(session, e):
        raise RuntimeError("always broken")

    topic = env.topic(Topic.ORDER_EVENTS)
    e = _ev()
    async with running(lambda stop: run_service(env, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        await publish(env.kafka_bootstrap, topic, e)
        msgs = await read_all(env.kafka_bootstrap, dlq_topic(topic, GROUP), wait=40)
    assert len(msgs) == 1
    hdr = {k: v.decode() for k, v in msgs[0].headers}
    assert "always broken" in hdr["error"]
    assert hdr["origin_topic"] == topic
    assert msgs[0].key == b"o1"


async def test_poison_message_goes_straight_to_dlq(env):
    from aiokafka import AIOKafkaProducer

    async def handler(session, e):
        raise AssertionError("must not run")

    topic = env.topic(Topic.ORDER_EVENTS)
    async with running(lambda stop: run_service(env, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        p = AIOKafkaProducer(bootstrap_servers=env.kafka_bootstrap)
        await p.start()
        await p.send_and_wait(topic, value=b"{not json", key=b"x")
        await p.stop()
        msgs = await read_all(env.kafka_bootstrap, dlq_topic(topic, GROUP), wait=20)
    assert len(msgs) == 1


async def _async(v):
    return v


async def test_replay_tool_resends_dlq_messages_after_fix(env):
    from darkstore_common.replay import replay_dlq

    broken = {"on": True}
    done = []

    async def handler(session, e):
        if broken["on"]:
            raise RuntimeError("bug")
        done.append(e.event_id)

    topic = env.topic(Topic.ORDER_EVENTS)
    e = _ev()
    async with running(lambda stop: run_service(env, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        await publish(env.kafka_bootstrap, topic, e)
        assert len(await read_all(env.kafka_bootstrap, dlq_topic(topic, GROUP), wait=40)) == 1
        broken["on"] = False  # "deploy the fix"
        assert await replay_dlq(env.kafka_bootstrap, dlq_topic(topic, GROUP)) == 1
        await until(lambda: _async(e.event_id in done), timeout=20)
    assert done == [e.event_id]


async def test_permanent_error_goes_to_dlq_without_retries(env):
    calls = []

    async def handler(session, e):
        calls.append(1)
        raise KeyError("missing field")  # deterministic: retrying cannot help

    topic = env.topic(Topic.ORDER_EVENTS)
    async with running(lambda stop: run_service(env, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        await publish(env.kafka_bootstrap, topic, _ev())
        msgs = await read_all(env.kafka_bootstrap, dlq_topic(topic, GROUP), wait=10)
    assert len(msgs) == 1 and len(calls) == 1
    assert "permanent" in dict(msgs[0].headers)["error"].decode()


async def test_hostile_headers_cannot_stall_crash_or_redirect(env):
    done = []

    async def handler(session, e):
        if e.order_id == "bad":
            raise RuntimeError("boom")
        done.append(e.order_id)

    topic = env.topic(Topic.ORDER_EVENTS)
    evil = {"not_before": "1e18", "attempt": "-5", "origin_topic": "evil.topic"}
    async with running(lambda stop: run_service(env, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        await publish(env.kafka_bootstrap, topic, _ev("bad"), headers=evil)
        await publish(env.kafka_bootstrap, topic, _ev("good"), headers={"not_before": "abc",
                                                                         "attempt": "x"})
        await until(lambda: _async("good" in done), timeout=20)  # consumer is alive, not stalled
        msgs = await read_all(env.kafka_bootstrap, dlq_topic(topic, GROUP), wait=40)
    assert [m.key for m in msgs] == [b"bad"]  # landed in the REAL origin's DLQ, not "evil.topic"
    assert dict(msgs[0].headers)["origin_topic"] == topic.encode()


async def test_unsupported_schema_version_goes_to_dlq(env):
    from darkstore_common.envelope import Envelope

    async def handler(session, e):
        raise AssertionError("must not run")

    topic = env.topic(Topic.ORDER_EVENTS)
    future = Envelope(event_type="order.cancelled", order_id="o1", schema_version=2, payload={})
    async with running(lambda stop: run_service(env, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        await publish(env.kafka_bootstrap, topic, future)
        assert len(await read_all(env.kafka_bootstrap, dlq_topic(topic, GROUP), wait=15)) == 1


async def test_shutdown_is_prompt_while_a_retry_is_pending(env):
    import time

    attempts = []

    async def handler(session, e):
        attempts.append(1)
        raise RuntimeError("transient")

    slow = env.model_copy(update={"retry_delays": ("60s",)})
    topic = slow.topic(Topic.ORDER_EVENTS)
    t0 = None
    async with running(lambda stop: run_service(slow, meta=service_meta("ctest"), group=GROUP,
                                                consumes=[Topic.ORDER_EVENTS], handler=handler,
                                                stop=stop)):
        await publish(slow.kafka_bootstrap, topic, _ev())
        await until(lambda: _async(len(attempts) == 1))
        await asyncio.sleep(3)  # message now sits on the 60s retry topic
        t0 = time.time()
    assert time.time() - t0 < 10  # used to block for the whole backoff
