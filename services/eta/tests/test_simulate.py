import numpy as np
import pandas as pd

from darkstore_eta.simulate import STAGES, generate_trips


def test_generation_is_deterministic_per_seed_and_differs_across_seeds():
    a, b, c = generate_trips(300, seed=1), generate_trips(300, seed=1), generate_trips(300, seed=2)
    pd.testing.assert_frame_equal(a, b)
    assert not a["target_min"].equals(c["target_min"])


def test_four_snapshots_per_trip_and_sorted_by_order_time():
    df = generate_trips(200, seed=3)
    assert len(df) == 200 * 4 and df.groupby("order_id").size().eq(4).all()
    assert set(df["stage"]) == set(range(len(STAGES)))
    firsts = df[df.stage == 0]["order_ts"].to_numpy()
    assert (np.diff(firsts) >= 0).all()          # time-ordered: needed for a time-based split


def test_remaining_time_shrinks_through_the_stages_of_one_trip():
    df = generate_trips(200, seed=4)
    for _, g in df.groupby("order_id"):
        g = g.sort_values("stage")
        assert g["target_min"].is_monotonic_decreasing and (g["target_min"] > 0).all()
        assert g["elapsed_min"].is_monotonic_increasing


def test_features_never_leak_the_future():
    df = generate_trips(300, seed=5)
    early = df[df.stage <= 1]
    assert early["pick_minutes"].isna().all()                 # pick time unknown until picking ends
    assert df[df.stage >= 2]["pick_minutes"].notna().all()
    # the actual rider only becomes known at assignment: the estimate before is a different column
    assert (df["rider_known"] == (df["stage"] >= 2).astype(int)).all()


def test_world_effects_are_present_rain_and_rush_hour_slow_deliveries():
    df = generate_trips(6000, seed=6)
    last = df[df.stage == 3].copy()
    last["pace"] = last["target_min"] / (last["dist_km"].clip(lower=0.2))
    assert last[last.raining == 1]["pace"].mean() > last[last.raining == 0]["pace"].mean() * 1.1
    rush = last[last.hour.between(19, 22)]["pace"].mean()
    quiet = last[last.hour.between(13, 16)]["pace"].mean()
    assert rush > quiet * 1.15
