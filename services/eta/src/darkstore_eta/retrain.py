"""Retrain on fresh simulated trips and promote ONLY if the candidate is demonstrably better.

Gate (all must hold): finite metrics, P90 coverage within +-5 points of 90%, and MAE at least
`tolerance` (default 1%) lower than the live model on the SAME held-out slice (each model uses its
own cell statistics). A candidate trained on the same seed as the live model is refused (its data
would overlap). Promotion is one atomic symlink swap of models/eta/current, so a running service
never sees a half-written or mixed (p50 from one version, p90 from another) model.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

from .features import attach_cell_stats
from .simulate import generate_trips
from .train import TARGET, link_current, metrics_for, split_by_time, train


def evaluate_bundle(bundle: Path, test_df) -> dict:
    from .predictor import _read_bundle        # verifies digests before unpickling
    b = _read_bundle(bundle)
    df = attach_cell_stats(test_df, b.meta["cell_stats"], b.meta["global_slowness"])
    p50 = np.maximum(b.p50.predict(df), 0.5)
    p90 = np.maximum(b.p90.predict(df) + b.meta["p90_offset"], p50 + 0.1)
    return metrics_for(df[TARGET].to_numpy(), p50, p90)


def passes_gate(m: dict, cur_mae: float | None, tolerance: float,
                coverage_band: float = 0.05) -> tuple[bool, str]:
    if not all(math.isfinite(v) for v in m.values()):
        return False, "non-finite metrics"
    if abs(m["p90_coverage"] - 0.9) > coverage_band:
        return False, (f"P90 coverage {m['p90_coverage']:.1%} outside "
                       f"{0.9 - coverage_band:.0%}-{0.9 + coverage_band:.0%}")
    if cur_mae is not None and not m["mae"] <= cur_mae * (1 - tolerance):
        return False, f"MAE {m['mae']:.2f} not at least {tolerance:.0%} better than current {cur_mae:.2f}"
    return True, "ok"


def retrain(models_dir: Path | str = "models/eta", trips: int = 40000, seed: int | None = None,
            n_estimators: int = 2000, mlflow_uri: str | None = None, tolerance: float = 0.01,
            coverage_band: float = 0.05) -> dict:
    models_dir = Path(models_dir)
    current = models_dir / "current"
    cur_seed = None
    if (current / "meta.json").exists():
        cur_seed = json.loads((current / "meta.json").read_text()).get("seed")
    seed = int(time.time()) % 100000 if seed is None else seed
    if seed == cur_seed:
        seed += 1                                   # never reuse the live model's data
    df = generate_trips(trips, seed=seed)
    version = f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}-s{seed}"
    vdir = models_dir / "versions" / version
    report = train(df, vdir, seed=seed, n_estimators=n_estimators, mlflow_uri=mlflow_uri, version=version)
    _, _, test = split_by_time(df)
    m = report["test"]["model"]
    cur_mae = evaluate_bundle(current, test)["mae"] if (current / "meta.json").exists() else None
    ok, why = passes_gate(m, cur_mae, tolerance, coverage_band)
    out = {"candidate": version, "candidate_mae": m["mae"], "current_mae": cur_mae,
           "candidate_p90_coverage": m["p90_coverage"], "promoted": ok, "reason": why,
           "mlflow": report.get("mlflow")}
    if ok:
        link_current(models_dir, vdir)
    if mlflow_uri and report.get("mlflow", {}).get("run_id"):
        try:
            import mlflow
            mlflow.set_tracking_uri(mlflow_uri)
            with mlflow.start_run(run_id=report["mlflow"]["run_id"]):
                mlflow.set_tags({"promoted": str(ok), "gate_reason": why,
                                 "current_mae_same_holdout": str(cur_mae)})
        except Exception:  # noqa: BLE001 - tagging is best effort
            pass
    return out


def cli() -> None:
    ap = argparse.ArgumentParser(description="Retrain the ETA model; promote only if it is better")
    ap.add_argument("--models-dir", default="models/eta")
    ap.add_argument("--trips", type=int, default=40000)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--mlflow-uri", default=os.environ.get("MLFLOW_TRACKING_URI"))
    a = ap.parse_args()
    print(json.dumps(retrain(a.models_dir, a.trips, a.seed, mlflow_uri=a.mlflow_uri), indent=2))


if __name__ == "__main__":
    cli()
