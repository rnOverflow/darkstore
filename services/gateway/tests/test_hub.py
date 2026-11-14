"""Hub behaviours that need no network: overflow verdict and the post-snapshot id floor."""
import pytest

from darkstore_common.tokens import Claims
from darkstore_gateway.hub import Conn, Hub
from darkstore_gateway.queues import OutQueue


def conn(max_total=2000):
    c = Conn(ws=None, claims=Claims("customer", "o1", 0))
    c.queue = OutQueue(max_droppable=50, max_total=max_total)
    return c


def msg(i, kind="status"):
    return {"order_id": "o1", "id": f"{i}-0", "kind": kind, "data": {"status": "created", "at": "t"}}


def test_a_connection_that_overflows_is_marked_and_woken_so_its_socket_gets_closed():
    hub, c = Hub(), conn(max_total=3)
    hub.add(c, "o1")
    for i in range(1, 8):
        hub.route(msg(i))
    assert c.overflow is True
    assert c.queue._wake.is_set()                 # the sender is woken to raise and close


def test_events_at_or_below_the_snapshot_id_are_not_pushed_after_the_flush():
    hub, c = Hub(), conn()
    c.syncing["o1"] = []
    hub.add(c, "o1")
    hub.route(msg(9))                              # arrives while the snapshot loads
    hub.route(msg(12))
    hub.flush_synced(c, "o1", snapshot_id="10-0")
    hub.route(msg(10))                             # a late duplicate of what the snapshot holds
    hub.route(msg(11))
    got = []
    while (m := c.queue.pop()):
        got.append(m["id"])
    assert got == ["12-0", "11-0"]


def test_per_subject_connection_count():
    hub, a, b = Hub(), conn(), conn()
    hub.admit(a)
    hub.admit(b)
    assert hub.subject_count("o1") == 2 and hub.subject_count("ops") == 0
    hub.release(a)
    assert hub.subject_count("o1") == 1


def test_the_store_and_destination_event_is_never_dropped_under_backpressure():
    # positions are superseded by the next one and may be dropped; the order's store/destination are sent once
    hub, c = Hub(), conn()
    hub.add(c, "o1")
    hub.route({"order_id": "o1", "id": "1-0", "kind": "meta",
               "data": {"store": {"id": "s", "lat": 1, "lng": 2}, "dest": {"lat": 3, "lng": 4}}})
    for i in range(2, 80):                         # far more positions than max_droppable (50)
        hub.route({"order_id": "o1", "id": f"{i}-0", "kind": "position",
                   "data": {"rider_id": "r", "lat": 1, "lng": 2, "at": "t"}})
    kinds = []
    while (m := c.queue.pop()):
        kinds.append(m["kind"])
    assert c.queue.dropped > 0 and "meta" in kinds
