import math

from darkstore_common.simclock import SimClock


def test_clock_maps_real_seconds_to_sim_time():
    c = SimClock(epoch=1000.0, start_hour=18.0, speed=60.0)        # 1 real s = 1 sim minute
    assert c.hour(1000.0) == 18.0
    assert math.isclose(c.hour(1000.0 + 30), 18.5)
    assert c.sim_minutes(30) == 30.0
    assert math.isclose(c.real_seconds(30.0), 30.0)


def test_clock_wraps_the_day_and_counts_days():
    c = SimClock(epoch=0.0, start_hour=23.0, speed=3600.0)         # 1 real s = 1 sim hour
    assert c.hour(2) == 1.0 and c.day(0) == 0 and c.day(2) == 1 and c.day(26) == 2
