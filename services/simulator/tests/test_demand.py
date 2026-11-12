from darkstore_simulator.demand import orders_per_minute


def test_rush_hours_exceed_off_peak():
    base = 10
    dinner, lunch, morning = (orders_per_minute(h, base) for h in (20.5, 13.0, 9.0))
    midday_lull, night = orders_per_minute(15.5, base), orders_per_minute(3.0, base)
    assert dinner > lunch > midday_lull > night
    assert morning > midday_lull


def test_rate_is_positive_and_wraps_around_24h():
    assert orders_per_minute(3.0, 10) > 0
    assert orders_per_minute(27.0, 10) == orders_per_minute(3.0, 10)
