import json
import math

import pytest

from darkstore_eta.heuristic import heuristic
from darkstore_eta.predictor import EtaPredictor
from darkstore_eta.simulate import generate_trips
from darkstore_eta.train import train

FEATS = dict(stage=0, elapsed_min=0.0, dist_km=2.5, rider_to_store_km=1.2, idle_riders_near=3,
             active_orders_in_store=6, picker_queue_depth=1, hour=20.0, dow=2, raining=0,
             pick_minutes=None, customer_cell="88618c2e5dfffff")


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("m")
    train(generate_trips(2500, seed=3), out_dir=out, seed=3, n_estimators=60)
    return out


def test_model_prediction_is_used_when_enabled_and_loaded(model_dir):
    eta = EtaPredictor(model_dir, enabled=True).predict(FEATS)
    assert eta.source == "model" and eta.model_version
    assert 0 < eta.p50 < eta.p90 < 120


def test_flag_off_uses_the_heuristic_exactly(model_dir):
    p = EtaPredictor(model_dir, enabled=False)
    eta = p.predict(FEATS)
    h = heuristic(stage=0, elapsed_min=0.0, dist_km=2.5, rider_to_store_km=1.2, pick_minutes=None)
    assert eta.source == "heuristic" and math.isclose(eta.p50, h[0]) and math.isclose(eta.p90, h[1])
    assert p.fallbacks["disabled"] == 1


def test_missing_model_falls_back(tmp_path):
    p = EtaPredictor(tmp_path / "nope", enabled=True)
    assert p.predict(FEATS).source == "heuristic" and p.fallbacks["no_model"] == 1


def test_corrupt_model_file_falls_back_and_never_raises(model_dir, tmp_path):
    import shutil
    bad = tmp_path / "bad"
    shutil.copytree(model_dir, bad)
    (bad / "p50.joblib").write_bytes(b"not a model")
    p = EtaPredictor(bad, enabled=True)
    assert p.predict(FEATS).source == "heuristic"


def test_model_blowing_up_at_predict_time_falls_back(model_dir):
    import dataclasses
    p = EtaPredictor(model_dir, enabled=True)
    broken = type("Broken", (), {"predict": lambda self, X: (_ for _ in ()).throw(RuntimeError("boom"))})()
    p._bundle = dataclasses.replace(p._bundle, p50=broken)
    assert p.predict(FEATS).source == "heuristic" and p.fallbacks["error"] == 1


def test_a_tampered_pickle_is_rejected_by_its_digest_before_it_is_loaded(model_dir, tmp_path):
    import shutil
    bad = tmp_path / "tampered"
    shutil.copytree(model_dir, bad)
    original = (bad / "p90.joblib").read_bytes()
    (bad / "p90.joblib").write_bytes(original + b"\x00")          # same format, different bytes
    p = EtaPredictor(bad, enabled=True)
    assert p.version is None and p.predict(FEATS).source == "heuristic"


def test_failed_reload_keeps_the_previous_working_model(model_dir, tmp_path):
    import shutil
    d = tmp_path / "live"
    shutil.copytree(model_dir, d)
    p = EtaPredictor(d, enabled=True)
    v = p.version
    (d / "p50.joblib").write_bytes(b"garbage")
    meta = json.loads((d / "meta.json").read_text())
    (d / "meta.json").write_text(json.dumps({**meta, "version": "broken"}))   # touches mtime
    assert p.reload_if_changed() is False and p.version == v
    assert p.predict(FEATS).source == "model"


@pytest.mark.parametrize("garbage", [None, "x", 5, [], {"stage": "x", "dist_km": "y"}, {"stage": None}])
def test_predict_never_raises_on_garbage(model_dir, garbage):
    eta = EtaPredictor(model_dir, enabled=True).predict(garbage)
    assert eta.source == "heuristic" and math.isfinite(eta.p50) and math.isfinite(eta.p90)


@pytest.mark.parametrize("bad", [dict(dist_km=-1), dict(dist_km=float("nan")), dict(stage=9), dict(dist_km=900)])
def test_invalid_or_out_of_range_inputs_use_the_heuristic(model_dir, bad):
    p = EtaPredictor(model_dir, enabled=True)
    assert p.predict({**FEATS, **bad}).source == "heuristic"
    assert p.fallbacks["bad_input"] == 1


def test_unseen_cell_uses_the_global_prior_not_an_error(model_dir):
    assert EtaPredictor(model_dir, enabled=True).predict({**FEATS, "customer_cell": "zzz"}).source == "model"


def test_hot_reload_picks_up_a_newly_trained_model(model_dir, tmp_path):
    import shutil
    d = tmp_path / "live"
    shutil.copytree(model_dir, d)
    p = EtaPredictor(d, enabled=True)
    v1 = p.version
    train(generate_trips(2500, seed=99), out_dir=d, seed=99, n_estimators=60)
    assert p.reload_if_changed() is True and p.version != v1
    assert p.reload_if_changed() is False and p.reload_if_changed() is False
