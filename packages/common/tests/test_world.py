import random

from darkstore_common.geo import cell_of
from darkstore_common.world import (cell_speed_factor, is_raining, pick_minutes, speed_multiplier,
                                    weather_speed_factor)
from darkstore_common.city import CENTER


def test_rain_is_deterministic_per_day_and_about_a_quarter_of_days():
    assert is_raining(17) == is_raining(17)
    share = sum(is_raining(d) for d in range(4000)) / 4000
    assert 0.20 < share < 0.30


def test_rain_slows_riders():
    assert weather_speed_factor(True) < weather_speed_factor(False) == 1.0


def test_cell_factor_is_stable_bounded_and_varies_across_cells():
    c = cell_of(*CENTER)
    assert cell_speed_factor(c) == cell_speed_factor(c)
    cells = {cell_of(CENTER[0] + i * 0.004, CENTER[1] + j * 0.004) for i in range(-8, 9) for j in range(-8, 9)}
    factors = [cell_speed_factor(x) for x in cells]
    assert all(0.7 <= f <= 1.1 for f in factors)
    assert max(factors) - min(factors) > 0.2          # some areas really are slower


def test_speed_multiplier_combines_traffic_weather_and_area():
    c = cell_of(*CENTER)
    dry_off_peak = speed_multiplier(hour=14.0, raining=False, cell=c)
    wet_rush = speed_multiplier(hour=9.0, raining=True, cell=c)
    assert wet_rush < dry_off_peak


def test_pick_time_grows_with_queue_and_rain_and_is_positive():
    rng = random.Random(1)
    calm = [pick_minutes(rng, queue_depth=0, raining=False) for _ in range(3000)]
    busy = [pick_minutes(rng, queue_depth=6, raining=True) for _ in range(3000)]
    assert min(calm) > 0 and sum(busy) / 3000 > 1.5 * sum(calm) / 3000
