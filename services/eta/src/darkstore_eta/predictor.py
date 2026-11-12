"""In-process ETA serving behind a feature flag, with a heuristic safety net.

predict() NEVER raises: if the flag is off, the model is missing/corrupt, inputs are invalid or
out of range, the model errors, or it returns a non-finite value, the heuristic answers instead and
the reason is counted in ``fallbacks`` (exported as a metric in P5).

A loaded model is ONE immutable ``Bundle``; reload swaps a single reference, so a concurrent
predict() always sees a consistent (p50, p90, offset, cell stats) set. A failed reload keeps the
previous working bundle.

SECURITY: bundles are joblib (pickle) files. Their SHA-256 digests in meta.json are verified before
unpickling, and the compose mount is read-only, but anyone who can write meta.json AND the pickles
can still run code: only point this at a directory you control.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import joblib
import pandas as pd

from .features import attach_cell_stats
from .heuristic import heuristic

log = logging.getLogger("darkstore.eta")


@dataclass(frozen=True)
class Eta:
    p50: float
    p90: float
    source: str                      # "model" | "heuristic"
    model_version: str | None = None


@dataclass(frozen=True)
class Bundle:
    p50: object
    p90: object
    meta: dict
    token: str                       # resolved directory: identifies which published version this is

    @property
    def version(self) -> str | None:
        return self.meta.get("version")


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _bad(f: dict) -> bool:
    try:
        if f["stage"] not in (0, 1, 2, 3):
            return True
        if not all(_num(f[k]) for k in ("elapsed_min", "dist_km", "rider_to_store_km", "hour")):
            return True
        return not (0 <= f["dist_km"] <= 60 and 0 <= f["rider_to_store_km"] <= 60
                    and f["elapsed_min"] >= 0)
    except (KeyError, TypeError):
        return True


def _read_bundle(directory: Path) -> Bundle:
    d = directory.resolve()           # resolve a `current` symlink ONCE: all files from one version
    meta = json.loads((d / "meta.json").read_text())
    for name, want in meta["sha256"].items():
        if hashlib.sha256((d / name).read_bytes()).hexdigest() != want:
            raise ValueError(f"{name} does not match the digest in meta.json")
    meta["_mtime"] = (d / "meta.json").stat().st_mtime_ns
    return Bundle(joblib.load(d / "p50.joblib"), joblib.load(d / "p90.joblib"), meta, str(d))


class EtaPredictor:
    def __init__(self, model_dir: Path | str | None, enabled: bool = True):
        self.dir = Path(model_dir) if model_dir else None
        self.enabled = enabled
        self.fallbacks: Counter[str] = Counter()
        self._bundle: Bundle | None = None
        self.load()

    @property
    def version(self) -> str | None:
        b = self._bundle
        return b.version if b else None

    def load(self) -> bool:
        if not self.dir or not (self.dir / "meta.json").exists():
            return False
        try:
            self._bundle = _read_bundle(self.dir)       # single reference swap
        except Exception as e:  # noqa: BLE001 - a bad bundle never takes down serving
            log.error("could not load ETA model from %s (keeping %s): %r", self.dir, self.version, e)
            return False
        log.info("loaded ETA model %s", self._bundle.version)
        return True

    def reload_if_changed(self) -> bool:
        """Pick up a newly promoted version (the `current` symlink moved or meta.json changed)."""
        if not self.dir:
            return False
        try:
            token = str(self.dir.resolve())
            mtime = (self.dir.resolve() / "meta.json").stat().st_mtime_ns
        except OSError:
            return False
        b = self._bundle
        if b and token == b.token and b.meta.get("_mtime") == mtime:
            return False
        return self.load()

    def _heuristic(self, f, reason: str) -> Eta:
        self.fallbacks[reason] += 1
        try:
            p50, p90 = heuristic(stage=f.get("stage", 0), elapsed_min=f.get("elapsed_min", 0.0),
                                 dist_km=f.get("dist_km"), rider_to_store_km=f.get("rider_to_store_km"),
                                 pick_minutes=f.get("pick_minutes"))
            if not (math.isfinite(p50) and math.isfinite(p90)):
                raise ValueError("non-finite heuristic")
        except Exception:  # noqa: BLE001 - garbage in: answer with constants, never raise
            p50, p90 = 15.0, 25.0
        return Eta(p50, p90, "heuristic", None)

    def predict(self, f: dict) -> Eta:
        if not isinstance(f, dict):
            return self._heuristic({}, "bad_input")
        if not self.enabled:
            return self._heuristic(f, "disabled")
        b = self._bundle                                # read once: consistent for this call
        if b is None:
            return self._heuristic(f, "no_model")
        if _bad(f):
            return self._heuristic(f, "bad_input")
        try:
            pick = f.get("pick_minutes")
            row = pd.DataFrame([{**f, "rider_known": int(f["stage"] >= 2),
                                 "pick_minutes": float("nan") if pick is None else pick}])
            row = attach_cell_stats(row, b.meta["cell_stats"], b.meta["global_slowness"])
            p50 = max(float(b.p50.predict(row)[0]), 0.5)
            p90 = max(float(b.p90.predict(row)[0]) + b.meta["p90_offset"], p50 + 0.1)
        except Exception as e:  # noqa: BLE001
            log.warning("ETA model failed, using heuristic: %r", e)
            return self._heuristic(f, "error")
        if not (math.isfinite(p50) and math.isfinite(p90)):
            return self._heuristic(f, "non_finite")
        return Eta(p50, p90, "model", b.version)
