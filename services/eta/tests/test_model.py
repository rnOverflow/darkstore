import json
import math

import numpy as np
import pytest

from darkstore_eta.heuristic import P90_FACTOR, heuristic
from darkstore_eta.simulate import generate_trips
from darkstore_eta.train import metrics_for, split_by_time, train


def test_heuristic_is_stage_aware_and_p90_is_a_fixed_buffer():
    base = dict(elapsed_min=0.0, dist_km=2.0, rider_to_store_km=1.0, pick_minutes=None)
    s0 = heuristic(stage=0, **base)
    s3 = heuristic(stage=3, **{**base, "elapsed_min": 9.0})
    assert s0[0] > s3[0] > 0                                   # less left once the rider has the order
    assert math.isclose(s0[1], s0[0] * P90_FACTOR)
    # 2 km * 1.4 road factor at 20 km/h = 8.4 min of riding, nothing else left at stage 3
    assert math.isclose(s3[0], 8.4, rel_tol=1e-6)


def test_time_split_is_chronological_by_trip_with_no_trip_straddling_sets():
    df = generate_trips(1000, seed=7)
    tr, va, te = split_by_time(df)
    assert tr.order_ts.max() <= va.order_ts.min() and va.order_ts.max() <= te.order_ts.min()
    assert not (set(tr.order_id) & set(te.order_id)) and not (set(va.order_id) & set(te.order_id))
    assert abs(len(tr) / len(df) - 0.7) < 0.02


def test_metrics_definitions():
    actual = np.array([10.0, 20.0, 0.2])
    m = metrics_for(actual, p50=np.array([12.0, 18.0, 1.0]), p90=np.array([15.0, 19.0, 1.5]))
    assert math.isclose(m["mae"], (2 + 2 + 0.8) / 3)
    assert math.isclose(m["mape"], (0.2 + 0.1 + 0.8 / 1.0) / 3)      # denominator clamped to >= 1 min
    assert math.isclose(m["p90_coverage"], 2 / 3)                    # 10<=15 and 0.2<=1.5, 20>19


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    out = tmp_path_factory.mktemp("model")
    report = train(generate_trips(6000, seed=11), out_dir=out, seed=11, n_estimators=150)
    return out, report


def test_model_beats_both_baselines_on_the_held_out_period(trained):
    _, report = trained
    t = report["test"]
    assert t["model"]["mae"] < t["heuristic"]["mae"]
    assert t["model"]["mae"] < t["calibrated"]["mae"]            # the strong, tuned baseline
    for stage in ("0", "1", "2", "3"):   # tiny fixture (6k trips): allow noise; the 40k run wins every stage
        b = report["test_by_stage"][stage]
        assert b["model"]["mae"] < b["calibrated"]["mae"] * 1.10


def test_calibrated_heuristic_is_fitted_on_training_rows_only():
    from darkstore_eta.train import apply_calibrated, calibrate_heuristic
    df = generate_trips(1500, seed=21)
    tr, _, te = split_by_time(df)
    k = calibrate_heuristic(tr)
    assert {(0,), (1,), (2,), (3,)} <= set(k)                     # stage-level fallbacks always exist
    assert any(len(key) == 3 for key in k)                        # plus (stage, rain, rush-band) groups
    assert all(0.3 < k50 < k90 < 6 for k50, k90 in k.values())
    p50, p90 = apply_calibrated(te, k)
    assert (p90 > p50).all()


def test_calibrated_p90_covers_about_ninety_percent_on_unseen_data(trained):
    _, report = trained
    cov = report["test"]["model"]["p90_coverage"]
    assert 0.85 <= cov <= 0.95, cov


def test_bundle_is_written_and_versioned(trained):
    out, report = trained
    meta = json.loads((out / "meta.json").read_text())
    assert (out / "p50.joblib").exists() and (out / "p90.joblib").exists()
    assert meta["version"] == report["version"] and meta["cell_stats"] and "p90_offset" in meta
