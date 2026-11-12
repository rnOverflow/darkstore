from darkstore_common.matching import Candidate, Weights, rank, score

STORE = (12.9716, 77.5946)
KM = 1 / 111.195  # degrees of latitude per km


def cand(rid, km_north, rating=4.5, idle=0.0, batch=0.0):
    return Candidate(rid, STORE[0] + km_north * KM, STORE[1], rating, idle, batch)


def order_of(cands):
    return [c.rider_id for _, _, c in rank(cands, *STORE)]


def test_closer_rider_wins_when_everything_else_equal():
    assert order_of([cand("far", 3), cand("near", 0.5)]) == ["near", "far"]


def test_much_better_rated_rider_can_beat_slightly_closer_one():
    assert order_of([cand("close_bad", 1.0, rating=2.0), cand("slightly_further_great", 1.3, rating=5.0)]) \
        == ["slightly_further_great", "close_bad"]


def test_fairness_longer_idle_wins_a_tie_on_distance():
    assert order_of([cand("fresh", 1, idle=10), cand("waiting", 1, idle=900)]) == ["waiting", "fresh"]


def test_idle_bonus_is_capped_so_distance_still_matters():
    w = Weights()
    far_long_idle = score(30.0, cand("a", 5, idle=10**6), w)
    near_normal = score(3.0, cand("b", 0.5), w)
    assert near_normal < far_long_idle


def test_batching_potential_lowers_score():
    assert order_of([cand("solo", 1), cand("batchable", 1, batch=1.0)]) == ["batchable", "solo"]


def test_ties_break_deterministically_by_rider_id():
    assert order_of([cand("b", 1), cand("a", 1)]) == ["a", "b"]
    assert order_of([cand("a", 1), cand("b", 1)]) == ["a", "b"]


def test_rank_returns_eta_minutes():
    (_, eta, _), = rank([cand("r", 1.0)], *STORE)
    assert 3.5 < eta < 5.0  # 1 km * 1.4 at ~20 km/h


def test_candidate_speed_changes_eta():
    slow = Candidate("s", STORE[0] + KM, STORE[1], speed_kmh=10)
    fast = Candidate("f", STORE[0] + KM, STORE[1], speed_kmh=30)
    (_, e_slow, _), (_, e_fast, _) = rank([slow], *STORE)[0], rank([fast], *STORE)[0]
    assert e_slow == pytest_approx(e_fast * 3)


def pytest_approx(v):
    import pytest
    return pytest.approx(v, rel=1e-6)
