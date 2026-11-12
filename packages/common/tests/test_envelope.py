import uuid
from datetime import timezone

import pytest
from pydantic import ValidationError

from darkstore_common.envelope import Envelope
from darkstore_common.events import make_event
from darkstore_common.topics import EVENT_TOPICS, Topic, dlq_topic, parse_delay, retry_topic


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


def test_every_lifecycle_event_has_a_topic():
    for ev in ["order.created", "inventory.reserved", "inventory.rejected", "picking.started",
               "picking.completed", "rider.assigned", "rider.picked_up",
               "order.out_for_delivery", "order.delivered", "order.cancelled"]:
        assert ev in EVENT_TOPICS
    assert EVENT_TOPICS["order.cancelled"] is Topic.ORDER_EVENTS


def test_retry_and_dlq_names_are_per_consumer_group():
    origin = "darkstore.order.events"
    assert retry_topic(origin, "inventory", "30s") == "darkstore.order.events.inventory.retry.30s"
    assert dlq_topic(origin, "inventory") == "darkstore.order.events.inventory.dlq"


def test_parse_delay():
    assert parse_delay("5s") == 5
    assert parse_delay("5m") == 300
    assert parse_delay("0.5s") == 0.5
    with pytest.raises(ValueError):
        parse_delay("soon")


def test_rider_events_carry_task_details_and_positions_are_typed():
    e = make_event("rider.assigned", "o1", rider_id="r1", store_lat=12.9, store_lng=77.6,
                   dest_lat=12.95, dest_lng=77.62, eta_to_store_s=240)
    assert e.payload["rider_id"] == "r1" and e.payload["eta_to_store_s"] == 240
    assert make_event("rider.delivered", "o1", rider_id="r1").payload == {"rider_id": "r1"}
    pos = make_event("rider.position", "o1", rider_id="r1", lat=12.9, lng=77.6)
    assert EVENT_TOPICS["rider.position"] is Topic.RIDER_POSITIONS
    assert pos.payload["lat"] == 12.9


def test_make_event_validates_payload():
    e = make_event("order.created", "o1", customer_id="c1", store_id="s1",
                   items=[{"sku": "milk", "qty": 2}])
    assert e.payload["items"] == [{"sku": "milk", "qty": 2}]
    with pytest.raises(ValidationError):
        make_event("order.created", "o1", customer_id="c1", store_id="s1",
                   items=[{"sku": "milk", "qty": 0}])
    with pytest.raises(KeyError):
        make_event("order.exploded", "o1")
