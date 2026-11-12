from darkstore_gateway.queues import Overflow, OutQueue, stream_id_key
import pytest


def msg(i, kind="position"):
    return {"id": f"{i}-0", "kind": kind}


def test_messages_come_out_in_arrival_order():
    q = OutQueue(max_droppable=10)
    for i in range(5):
        q.put(msg(i), droppable=True)
    assert [q.pop()["id"] for _ in range(5)] == [f"{i}-0" for i in range(5)]
    assert q.pop() is None


def test_overflow_drops_the_oldest_droppable_but_never_a_status():
    q = OutQueue(max_droppable=3)
    q.put(msg(1, "status"), droppable=False)
    for i in range(2, 9):                              # 7 positions into a bound of 3
        q.put(msg(i), droppable=True)
    got = []
    while (m := q.pop()):
        got.append((m["id"], m["kind"]))
    assert got[0] == ("1-0", "status")                 # the status survived every overflow
    assert [i for i, k in got if k == "position"] == ["6-0", "7-0", "8-0"]   # newest 3 kept
    assert q.dropped == 4


def test_status_events_are_never_dropped_even_under_a_flood_of_positions():
    q = OutQueue(max_droppable=2)
    expected = []
    for i in range(1, 200):
        if i % 10 == 0:
            q.put(msg(i, "status"), droppable=False)
            expected.append(f"{i}-0")
        else:
            q.put(msg(i), droppable=True)
    out = []
    while (m := q.pop()):
        if m["kind"] == "status":
            out.append(m["id"])
    assert out == expected


def test_a_client_that_never_reads_is_cut_off_instead_of_growing_forever():
    q = OutQueue(max_droppable=5, max_total=20)
    with pytest.raises(Overflow):
        for i in range(100):
            q.put(msg(i, "status"), droppable=False)


def test_stream_ids_compare_numerically_not_lexicographically():
    assert stream_id_key("9-0") < stream_id_key("10-0") < stream_id_key("10-1")
    assert stream_id_key("1731000000000-5") > stream_id_key("1731000000000-4")
