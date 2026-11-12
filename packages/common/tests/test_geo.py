import math

import h3

from darkstore_common.city import CENTER, STORES, nearest_store
from darkstore_common.geo import (HEAT_RES, MATCH_RES, cell_of, eta_minutes, haversine_km, ring)


def test_haversine_known_distance():
    # Bengaluru centre to roughly 1 degree of latitude north = ~111.2 km
    d = haversine_km(12.9716, 77.5946, 13.9716, 77.5946)
    assert math.isclose(d, 111.2, rel_tol=0.01)
    assert haversine_km(1, 1, 1, 1) == 0


def test_eta_applies_road_factor_and_speed():
    # 1 km straight line * 1.4 road factor at 21 km/h = 4 minutes
    lat2 = 12.9716 + 1 / 111.195
    assert math.isclose(eta_minutes(12.9716, 77.5946, lat2, 77.5946, speed_kmh=21), 4.0, rel_tol=0.02)
    assert eta_minutes(1, 1, 1, 1) == 0


def test_resolutions_match_spec():
    assert (MATCH_RES, HEAT_RES) == (8, 9)
    assert h3.get_resolution(cell_of(*CENTER)) == 8
    assert h3.get_resolution(cell_of(*CENTER, res=HEAT_RES)) == 9


def test_ring_is_exactly_distance_k():
    c = cell_of(*CENTER)
    assert ring(c, 0) == [c]
    r2 = ring(c, 2)
    assert len(r2) == 12 and all(h3.grid_distance(c, x) == 2 for x in r2)


def test_step_towards_moves_along_the_line_and_snaps_on_arrival():
    from darkstore_common.geo import step_towards
    lat, lng, arrived = step_towards(12.0, 77.0, 12.0 + 1 / 111.195 * 2, 77.0, km=0.5)
    assert not arrived and math.isclose(haversine_km(12.0, 77.0, lat, lng), 0.5, rel_tol=0.01)
    assert step_towards(12.0, 77.0, 12.0001, 77.0, km=0.5) == (12.0001, 77.0, True)
    assert step_towards(12.0, 77.0, 12.0, 77.0, km=0.5) == (12.0, 77.0, True)


def test_traffic_is_slower_in_rush_hour_and_sim_clock_advances():
    from darkstore_common.city import sim_hour, traffic_factor
    assert traffic_factor(9.0) < traffic_factor(14.0) and traffic_factor(19.0) < traffic_factor(3.0)
    assert sim_hour(18.0, speed=60.0, elapsed_real=3600) == (18.0 + 60.0) % 24  # wraps at 24h
    assert sim_hour(18.0, speed=60.0, elapsed_real=60) == 19.0
    assert 0 <= sim_hour(23.5, speed=3600.0, elapsed_real=1800) < 24


def test_city_has_six_stores_with_valid_cells_and_nearest_lookup():
    assert len(STORES) == 6
    for sid, (lat, lng) in STORES.items():
        assert nearest_store(lat, lng) == sid
