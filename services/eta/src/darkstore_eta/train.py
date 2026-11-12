"""Train, evaluate and publish the ETA model.

P50 and P90 are LightGBM quantile regressors inside scikit-learn pipelines. The split is
CHRONOLOGICAL by trip (train 70% / validation 15% / test 15%) so the test set is genuinely the
future. H3 cell statistics come from training rows only. The P90 is calibrated with a split-
conformal offset computed on the validation set, then everything is reported on the untouched test
set against the heuristic baseline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer

from .features import (FEATURES, attach_cell_stats, attach_cell_stats_oof, compute_cell_stats,
                       engineer)
from .heuristic import heuristic_frame
from .simulate import STAGES, generate_trips

TARGET = "target_min"


def metrics_for(actual: np.ndarray, p50: np.ndarray, p90: np.ndarray) -> dict[str, float]:
    err = np.abs(actual - p50)
    pin = np.where(actual >= p90, 0.9 * (actual - p90), 0.1 * (p90 - actual))
    return {"mae": float(err.mean()),
            "mape": float((err / np.maximum(actual, 1.0)).mean()),   # denominator clamped to >= 1 min
            "rmse": float(np.sqrt(((actual - p50) ** 2).mean())),
            "p90_coverage": float((actual <= p90).mean()),
            "p90_pinball": float(pin.mean()),
            "p90_mean_width": float((p90 - p50).mean())}


def split_by_time(df: pd.DataFrame, fracs=(0.70, 0.15)) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ids = df[df["stage"] == 0].sort_values("order_ts")["order_id"].tolist()
    a, b = int(len(ids) * fracs[0]), int(len(ids) * (fracs[0] + fracs[1]))
    part = {oid: 0 for oid in ids[:a]} | {oid: 1 for oid in ids[a:b]} | {oid: 2 for oid in ids[b:]}
    which = df["order_id"].map(part)
    return df[which == 0], df[which == 1], df[which == 2]


def _fit_quantile(Xtr, ytr, Xva, yva, alpha: float, seed: int, n_estimators: int) -> lgb.LGBMRegressor:
    m = lgb.LGBMRegressor(objective="quantile", alpha=alpha, n_estimators=n_estimators,
                          learning_rate=0.05, num_leaves=31, min_child_samples=40, subsample=0.8,
                          subsample_freq=1, colsample_bytree=0.9, random_state=seed, n_jobs=4,
                          verbose=-1)
    m.fit(Xtr, ytr, eval_X=Xva, eval_y=yva, eval_metric="quantile",
          callbacks=[lgb.early_stopping(40, verbose=False)])
    return m


def _band(hour: np.ndarray) -> np.ndarray:
    """0 normal, 1 rush (8-10, 17-21), 2 night (<6): the effects an engineer would spot first."""
    return np.where(((hour >= 8) & (hour < 10)) | ((hour >= 17) & (hour < 21)), 1,
                    np.where(hour < 6, 2, 0))


def _ctx_key(df: pd.DataFrame) -> list[tuple[int, int, int]]:
    return list(zip(df["stage"].astype(int), df["raining"].astype(int), _band(df["hour"].to_numpy()).astype(int)))


def calibrate_heuristic(tr: pd.DataFrame) -> dict:
    """The strong baseline: the same heuristic with two scale factors (median and 90th percentile
    of actual / heuristic) fitted on the TRAINING period for each (stage, raining, rush-hour band).
    That is what a careful engineer would do without ML: notice rain and rush hour and correct for
    them. Empty groups fall back to the stage-level factors."""
    h50, _ = heuristic_frame(tr)
    ratio = tr[TARGET].to_numpy() / np.maximum(h50, 0.5)
    keys = _ctx_key(tr)
    k: dict = {}
    for stage in range(len(STAGES)):
        m = (tr["stage"] == stage).to_numpy()
        k[(stage,)] = (float(np.median(ratio[m])), float(np.quantile(ratio[m], 0.9)))
    for key in set(keys):
        m = np.array([kk == key for kk in keys])
        if m.sum() >= 30:
            k[key] = (float(np.median(ratio[m])), float(np.quantile(ratio[m], 0.9)))
    return k


def apply_calibrated(df: pd.DataFrame, k: dict) -> tuple[np.ndarray, np.ndarray]:
    h50, _ = heuristic_frame(df)
    pairs = [k.get(key, k[(key[0],)]) for key in _ctx_key(df)]
    k50 = np.array([p[0] for p in pairs]); k90 = np.array([p[1] for p in pairs])
    return h50 * k50, np.maximum(h50 * k90, h50 * k50 + 0.1)


def trip_bootstrap_ci(te: pd.DataFrame, err_a: np.ndarray, err_b: np.ndarray, n: int = 400,
                      seed: int = 0) -> dict:
    """95% CI (resampling TRIPS, since a trip's 4 snapshots are correlated) for MAE of a, of b, and
    for the relative improvement of a over b."""
    codes, _ = pd.factorize(te["order_id"])
    k = codes.max() + 1
    cnt = np.bincount(codes, minlength=k).astype(float)
    sa, sb = np.bincount(codes, weights=err_a, minlength=k), np.bincount(codes, weights=err_b, minlength=k)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n, k))
    ma, mb = sa[idx].sum(1) / cnt[idx].sum(1), sb[idx].sum(1) / cnt[idx].sum(1)
    q = lambda v: [float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))]  # noqa: E731
    return {"model_mae_ci95": q(ma), "baseline_mae_ci95": q(mb), "improvement_ci95": q(1 - ma / mb)}


def _predict(g50, g90, X, offset) -> tuple[np.ndarray, np.ndarray]:
    p50 = np.maximum(g50.predict(X), 0.5)
    return p50, np.maximum(g90.predict(X) + offset, p50 + 0.1)


def train(df: pd.DataFrame, out_dir: Path | str, seed: int = 0, n_estimators: int = 2000,
          mlflow_uri: str | None = None, experiment: str = "darkstore-eta",
          version: str | None = None) -> dict:
    out_dir = Path(out_dir)
    tr, va, te = split_by_time(df)
    stats, glob = compute_cell_stats(tr)                       # training rows only: no leakage
    va, te = (attach_cell_stats(x, stats, glob) for x in (va, te))
    tr = attach_cell_stats_oof(tr)                              # out-of-fold: never its own target
    fe = FunctionTransformer(engineer, validate=False).fit(tr)
    Xtr, Xva, Xte = fe.transform(tr), fe.transform(va), fe.transform(te)

    g50 = _fit_quantile(Xtr, tr[TARGET], Xva, va[TARGET], 0.5, seed, n_estimators)
    g90 = _fit_quantile(Xtr, tr[TARGET], Xva, va[TARGET], 0.9, seed, n_estimators)
    offset = float(np.quantile(va[TARGET].to_numpy() - g90.predict(Xva), 0.90))   # split conformal

    p50, p90 = _predict(g50, g90, Xte, offset)
    raw90 = np.maximum(g90.predict(Xte), p50 + 0.1)
    h50, h90 = heuristic_frame(te)
    kcal = calibrate_heuristic(tr)
    c50, c90 = apply_calibrated(te, kcal)
    y = te[TARGET].to_numpy()
    report = {
        "version": version or f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}-s{seed}",
        "seed": seed, "features": FEATURES, "p90_offset": offset,
        "rows": {"train": len(tr), "val": len(va), "test": len(te)},
        "trips": {"train": tr.order_id.nunique(), "val": va.order_id.nunique(),
                  "test": te.order_id.nunique()},
        "test_period_days": [float(te.order_ts.min() / 86400), float(te.order_ts.max() / 86400)],
        "best_iteration": {"p50": int(g50.best_iteration_ or n_estimators),
                           "p90": int(g90.best_iteration_ or n_estimators)},
        "test": {"model": metrics_for(y, p50, p90), "heuristic": metrics_for(y, h50, h90),
                 "calibrated": metrics_for(y, c50, c90)},
        "bootstrap_vs_calibrated": trip_bootstrap_ci(te, np.abs(y - p50), np.abs(y - c50)),
        "p90_uncalibrated_coverage": float((y <= raw90).mean()),
        "test_by_stage": {},
        "importance_gain": dict(sorted(zip(FEATURES, map(float, g50.booster_.feature_importance("gain"))),
                                       key=lambda kv: -kv[1])[:8]),
    }
    for s in range(len(STAGES)):
        m = (te["stage"] == s).to_numpy()
        report["test_by_stage"][str(s)] = {"model": metrics_for(y[m], p50[m], p90[m]),
                                           "heuristic": metrics_for(y[m], h50[m], h90[m]),
                                           "calibrated": metrics_for(y[m], c50[m], c90[m])}
    _publish(out_dir, report, stats, glob, fe, g50, g90, offset)
    if mlflow_uri:
        report["mlflow"] = _log_mlflow(mlflow_uri, experiment, report, out_dir)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish(out_dir: Path, report: dict, stats, glob, fe, g50, g90, offset) -> None:
    """Write the bundle. meta.json goes LAST and carries SHA-256 digests of the pickles, which the
    server verifies before unpickling (a swapped/corrupted file is rejected, never executed)."""
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    joblib.dump(Pipeline([("fe", fe), ("gbm", g50)]), out_dir / "p50.joblib")
    joblib.dump(Pipeline([("fe", fe), ("gbm", g90)]), out_dir / "p90.joblib")
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    (out_dir / "report.md").write_text(render_report(report))
    tmp = out_dir / "meta.json.tmp"
    tmp.write_text(json.dumps({"version": report["version"], "seed": report["seed"],
                               "p90_offset": offset, "global_slowness": glob, "cell_stats": stats,
                               "features": FEATURES, "test": report["test"]["model"],
                               "sha256": {n: sha256(out_dir / n) for n in ("p50.joblib", "p90.joblib")}}))
    os.replace(tmp, out_dir / "meta.json")


def link_current(models_dir: Path | str, version_dir: Path | str) -> None:
    """Atomically point models/eta/current at a version directory (rename over a symlink).
    A pre-existing REAL directory called `current` is archived first."""
    models_dir, version_dir = Path(models_dir), Path(version_dir).resolve()
    cur = models_dir / "current"
    if cur.exists() and not cur.is_symlink():
        archive = models_dir / "versions" / f"legacy-{int(time.time())}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        cur.rename(archive)
    tmp = models_dir / f".current.{os.getpid()}"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(os.path.relpath(version_dir, models_dir))
    os.replace(tmp, cur)


def _log_mlflow(uri: str, experiment: str, report: dict, out_dir: Path) -> dict:
    try:
        import mlflow
        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment(experiment)
        with mlflow.start_run(run_name=report["version"]) as run:
            mlflow.log_params({"seed": report["seed"], "n_features": len(FEATURES),
                               "p90_offset": round(report["p90_offset"], 4),
                               **{f"rows_{k}": v for k, v in report["rows"].items()},
                               **{f"best_iter_{k}": v for k, v in report["best_iteration"].items()}})
            for who in ("model", "heuristic", "calibrated"):
                mlflow.log_metrics({f"test_{who}_{k}": v for k, v in report["test"][who].items()})
            for base in ("heuristic", "calibrated"):
                mlflow.log_metric(f"test_mae_improvement_vs_{base}_pct",
                                  100 * (1 - report["test"]["model"]["mae"] / report["test"][base]["mae"]))
            mlflow.log_artifacts(str(out_dir), artifact_path="model")
            return {"run_id": run.info.run_id, "uri": uri, "experiment": experiment}
    except Exception as e:  # noqa: BLE001 - tracking must not lose a good model
        return {"error": f"{type(e).__name__}: {e}"[:300]}


def render_report(r: dict) -> str:
    t = r["test"]
    lines = [f"# ETA model report: {r['version']}", "",
             f"Held-out test = the last 15% of trips by order time ({r['trips']['test']} trips, "
             f"{r['rows']['test']} state snapshots; days {r['test_period_days'][0]:.0f}-"
             f"{r['test_period_days'][1]:.0f}). Train {r['trips']['train']} / val {r['trips']['val']} trips. "
             "All numbers are on SIMULATED trips (see docs/ml/README.md).", "",
             "*Heuristic* = distance / 20 km/h x 1.4 road factor + 3 min pick, P90 = x1.35 (the production "
             "fallback). *Calibrated heuristic* = the same formula with two scale factors fitted on the "
             "training period per (stage, raining, rush-hour band): what a careful engineer would build "
             "without ML.", "",
             "| | Heuristic | Calibrated heuristic | LightGBM | vs calibrated |", "|---|---|---|---|---|"]

    def row(name, key, pct=False):
        f = (lambda v: f"{v:.1%}") if pct else (lambda v: f"{v:.2f}")
        h, c, m = (t[w][key] for w in ("heuristic", "calibrated", "model"))
        lines.append(f"| {name} | {f(h)} | {f(c)} | {f(m)} | {(m - c) / c:+.0%} |")
    row("MAE (min)", "mae"); row("MAPE", "mape", pct=True); row("RMSE (min)", "rmse")
    row("P90 pinball loss", "p90_pinball")
    lines.append("| P90 coverage (target 90%) | " + " | ".join(
        f"{t[w]['p90_coverage']:.1%}" for w in ("heuristic", "calibrated", "model")) + " | |")
    lines.append("| P90 mean width over P50 (min) | " + " | ".join(
        f"{t[w]['p90_mean_width']:.2f}" for w in ("heuristic", "calibrated", "model")) + " | |")
    ci = r["bootstrap_vs_calibrated"]
    lines += ["", f"95% CI by trip bootstrap: LightGBM MAE {ci['model_mae_ci95'][0]:.2f}-{ci['model_mae_ci95'][1]:.2f}, "
              f"calibrated heuristic {ci['baseline_mae_ci95'][0]:.2f}-{ci['baseline_mae_ci95'][1]:.2f}, "
              f"improvement {ci['improvement_ci95'][0]:.0%} to {ci['improvement_ci95'][1]:.0%}."]
    lines += ["", f"Uncalibrated LightGBM P90 coverage: {r['p90_uncalibrated_coverage']:.1%}; "
              f"conformal offset +{r['p90_offset']:.2f} min.", "", "**By stage (MAE minutes)**", "",
              "| Stage | Heuristic | Calibrated heuristic | LightGBM | LightGBM P90 coverage |",
              "|---|---|---|---|---|"]
    for s, name in enumerate(STAGES):
        b = r["test_by_stage"][str(s)]
        lines.append(f"| {name} | {b['heuristic']['mae']:.2f} | {b['calibrated']['mae']:.2f} | "
                     f"{b['model']['mae']:.2f} | {b['model']['p90_coverage']:.1%} |")
    lines += ["", "Top features (gain): " + ", ".join(r["importance_gain"])]
    return "\n".join(lines) + "\n"


def cli() -> None:
    ap = argparse.ArgumentParser(description="Generate trips, train and publish the ETA model")
    ap.add_argument("--trips", type=int, default=40000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--models-dir", default="models/eta")
    ap.add_argument("--parquet", default="data/eta/trips.parquet")
    ap.add_argument("--mlflow-uri", default=os.environ.get("MLFLOW_TRACKING_URI"))
    ap.add_argument("--report", default="docs/ml/eta-report.md")
    a = ap.parse_args()
    t0 = time.time()
    df = generate_trips(a.trips, seed=a.seed)
    Path(a.parquet).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(a.parquet, index=False)
    print(f"generated {a.trips} trips ({len(df)} snapshots) -> {a.parquet} in {time.time() - t0:.1f}s")
    t1 = time.time()
    version = f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}-s{a.seed}"
    vdir = Path(a.models_dir) / "versions" / version
    report = train(pd.read_parquet(a.parquet), vdir, seed=a.seed, mlflow_uri=a.mlflow_uri, version=version)
    link_current(a.models_dir, vdir)
    print(f"trained in {time.time() - t1:.1f}s -> {vdir} (current -> {version})")
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(render_report(report))
    print(render_report(report))
    print("mlflow:", report.get("mlflow", "not logged (no --mlflow-uri)"))


if __name__ == "__main__":
    cli()
