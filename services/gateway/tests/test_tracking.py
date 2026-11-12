from darkstore_gateway.tracking import RANK, derive


def test_status_events_map_to_forward_only_ranks():
    order = ["created", "reserved", "picking", "picked", "assigned", "out_for_delivery", "delivered"]
    ranks = [RANK[s] for s in order]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)
    assert RANK["cancelled"] == RANK["rejected"] == RANK["delivered"]      # all terminal


def test_derive_status_from_each_lifecycle_event():
    cases = {"order.created": "created", "inventory.reserved": "reserved",
             "inventory.rejected": "rejected", "picking.started": "picking",
             "picking.completed": "picked", "rider.assigned": "assigned",
             "order.out_for_delivery": "out_for_delivery", "order.delivered": "delivered",
             "order.cancelled": "cancelled"}
    for et, status in cases.items():
        kind, data, _ = derive(et, {}, "2026-01-01T00:00:00+00:00")
        assert kind == "status" and data["status"] == status and data["at"]


def test_order_created_carries_store_and_destination_meta():
    _, _, meta = derive("order.created", {"store_id": "store-0", "lat": 12.95, "lng": 77.6},
                        "2026-01-01T00:00:00+00:00")
    assert meta["store"]["id"] == "store-0" and meta["store"]["lat"] > 12 and meta["dest"] == {"lat": 12.95, "lng": 77.6}


def test_eta_and_position_events_are_reshaped_for_clients():
    kind, d, _ = derive("eta.updated", {"stage": "assigned", "p50_real_s": 40.5, "p90_real_s": 70.0,
                                        "source": "model", "p50_min": 20, "p90_min": 35,
                                        "model_version": "v", "elapsed_min": 3}, "2026-01-01T00:00:00+00:00")
    assert kind == "eta" and d == {"stage": "assigned", "p50_real_s": 40.5, "p90_real_s": 70.0,
                                   "source": "model", "at": "2026-01-01T00:00:00+00:00"}
    kind, d, _ = derive("rider.position", {"rider_id": "r1", "lat": 1.0, "lng": 2.0}, "2026-01-01T00:00:00+00:00")
    assert kind == "position" and d == {"rider_id": "r1", "lat": 1.0, "lng": 2.0, "at": "2026-01-01T00:00:00+00:00"}


def test_events_the_gateway_does_not_show_are_ignored():
    assert derive("rider.picked_up", {"rider_id": "r"}, "2026-01-01T00:00:00+00:00") is None
    assert derive("rider.delivered", {"rider_id": "r"}, "2026-01-01T00:00:00+00:00") is None
