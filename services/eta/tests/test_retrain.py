import json

from darkstore_eta.retrain import retrain
from darkstore_eta.simulate import generate_trips
from darkstore_eta.train import train


def test_first_retrain_promotes_via_an_atomic_symlink_to_a_versioned_dir(tmp_path):
    out = retrain(models_dir=tmp_path, trips=2500, seed=5, n_estimators=60, coverage_band=0.25)
    cur = tmp_path / "current"
    assert out["promoted"] and cur.is_symlink() and (cur / "meta.json").exists()
    assert json.loads((cur / "meta.json").read_text())["version"] == out["candidate"]
    assert cur.resolve() == (tmp_path / "versions" / out["candidate"]).resolve()


def test_gate_rejects_nonfinite_miscalibrated_and_not_better_candidates():
    from darkstore_eta.retrain import passes_gate
    good = dict(mae=3.0, p90_coverage=0.9)
    assert passes_gate(good, None, 0.01)[0]
    assert not passes_gate({**good, "mae": float("nan")}, None, 0.01)[0]
    assert not passes_gate({**good, "p90_coverage": 0.7}, None, 0.01)[0]
    assert passes_gate({**good, "p90_coverage": 0.7}, None, 0.01, coverage_band=0.25)[0]
    assert not passes_gate(good, 3.0, 0.01)[0]            # equal is not better
    assert not passes_gate(good, 3.01, 0.01)[0]           # better, but inside the 1% margin
    assert passes_gate(good, 3.5, 0.01)[0]


def test_candidate_never_reuses_the_live_models_seed(tmp_path):
    first = retrain(models_dir=tmp_path, trips=2500, seed=5, n_estimators=60, coverage_band=0.25)
    second = retrain(models_dir=tmp_path, trips=2500, seed=5, n_estimators=60, coverage_band=0.25)
    assert second["candidate"] != first["candidate"] and second["candidate"].endswith("-s6")


def test_candidate_that_is_worse_than_current_is_not_promoted(tmp_path):
    # a deliberately weak "current"... then a strong candidate wins
    train(generate_trips(2500, seed=1), out_dir=tmp_path / "current", seed=1, n_estimators=3)  # real dir
    out = retrain(models_dir=tmp_path, trips=2500, seed=2, n_estimators=80, coverage_band=0.25)
    assert out["promoted"] and out["candidate_mae"] < out["current_mae"]
    assert (tmp_path / "current").is_symlink()                    # the old real dir was archived
    assert any((tmp_path / "versions").glob("legacy-*"))


def test_a_candidate_trained_too_little_is_rejected_and_current_is_untouched(tmp_path):
    train(generate_trips(2500, seed=1), out_dir=tmp_path / "current", seed=1, n_estimators=80)
    before = (tmp_path / "current" / "meta.json").read_text()
    out = retrain(models_dir=tmp_path, trips=2500, seed=2, n_estimators=2, coverage_band=0.25)
    assert not out["promoted"] and out["candidate_mae"] > out["current_mae"] and out["reason"]
    assert (tmp_path / "current" / "meta.json").read_text() == before
