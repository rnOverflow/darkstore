import uuid
from datetime import timezone

import pytest
from pydantic import ValidationError

from darkstore_common.envelope import Envelope
from darkstore_common.topics import EVENT_TOPICS, Topic, dlq_topic, retry_topic


def test_envelope_defaults_generate_unique_ids_and_utc_time():
    a = Envelope(event_type="order.created", order_id="o1", payload={})
    b = Envelope(event_type="order.created", order_id="o1", payload={})
    assert a.event_id != b.event_id
    uuid.UUID(a.event_id)
    assert a.occurred_at.tzinfo is timezone.utc
    assert a.schema_version == 1


def test_partition_key_is_order_id():
    e = Envelope(event_type="order.created", order_id="o-42", payload={})
    assert e.partition_key() == b"o-42"


def test_envelope_rejects_empty_order_id():
    with pytest.raises(ValidationError):
        Envelope(event_type="order.created", order_id="", payload={})


def test_roundtrip_json():
    e = Envelope(event_type="picking.started", order_id="o1", payload={"store": "s1"})
    assert Envelope.model_validate_json(e.model_dump_json()) == e


def test_every_lifecycle_event_has_a_topic_and_retry_dlq_names():
    for ev in ["order.created", "inventory.reserved", "inventory.rejected", "picking.started",
               "picking.completed", "rider.assigned", "rider.picked_up",
               "order.out_for_delivery", "order.delivered", "order.cancelled"]:
        assert ev in EVENT_TOPICS
    t = Topic.ORDER_EVENTS
    assert retry_topic(t, "30s") == f"{t.value}.retry.30s"
    assert dlq_topic(t) == f"{t.value}.dlq"
