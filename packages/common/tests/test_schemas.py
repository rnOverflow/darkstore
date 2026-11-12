from darkstore_common.events import make_event
from darkstore_common.schemas import event_schema, subject
from darkstore_common.topics import EVENT_TOPICS


def test_every_event_has_a_schema_that_pins_its_type_and_payload():
    for ev in EVENT_TOPICS:
        s = event_schema(ev)
        assert s["properties"]["event_type"] == {"const": ev}
        assert "payload" in s["properties"]
        assert "event_id" in s["properties"]


def test_order_created_schema_requires_items():
    p = event_schema("order.created")["properties"]["payload"]
    assert "items" in p["required"]


def test_subject_naming():
    assert subject("darkstore", "inventory.reserved") == "darkstore.inventory.events-inventory.reserved"


def test_made_events_carry_schema_version_1():
    assert make_event("order.cancelled", "o1").schema_version == 1
