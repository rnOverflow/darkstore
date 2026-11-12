"""Feature engineering shared by training and serving (one code path = no train/serve skew)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from darkstore_common.geo import DEFAULT_SPEED_KMH, ROAD_FACTOR

FEATURES = ["stage", "elapsed_min", "dist_km", "rider_to_store_km", "rider_known",
            "idle_riders_near", "active_orders_in_store", "picker_queue_depth",
            "hour_sin", "hour_cos", "dow", "raining", "pick_minutes",
            "cell_slowness", "cell_count", "travel_base_min", "to_store_base_min"]
_SHRINK = 10.0   # cells with few observations are pulled toward the global mean


def compute_cell_stats(train: pd.DataFrame) -> tuple[dict[str, list[float]], float]:
    """H3 cell stats from TRAINING rows only: how much slower than the plain distance/speed
    estimate deliveries into a cell actually are (smoothed). Returns ({cell: [slowness, n]}, global)."""
    last = train[train["stage"] == 3]
    pace = last["target_min"] / (last["dist_km"].clip(lower=0.2) * ROAD_FACTOR / DEFAULT_SPEED_KMH * 60)
    glob = float(pace.mean())
    g = pace.groupby(last["customer_cell"]).agg(["sum", "count"])
    stats = {cell: [float((s + _SHRINK * glob) / (n + _SHRINK)), int(n)] for cell, (s, n) in
             g.iterrows()}
    return stats, glob


def attach_cell_stats_oof(train: pd.DataFrame, folds: int = 5) -> pd.DataFrame:
    """Training rows get cell stats computed WITHOUT their own fold (grouped by trip), so a rare
    cell's feature is never partly its own target. Val/test/serving use the full-train stats."""
    fold = train["order_id"].map(lambda o: int(__import__("hashlib").md5(o.encode()).hexdigest()[:6], 16) % folds)
    parts = []
    for k in range(folds):
        stats, glob = compute_cell_stats(train[fold != k])
        parts.append(attach_cell_stats(train[fold == k], stats, glob))
    return pd.concat(parts).loc[train.index]


def attach_cell_stats(df: pd.DataFrame, stats: dict[str, list[float]], glob: float) -> pd.DataFrame:
    out = df.copy()
    looked = out["customer_cell"].map(stats)
    out["cell_slowness"] = looked.map(lambda v: v[0] if isinstance(v, list) else glob)
    out["cell_count"] = looked.map(lambda v: v[1] if isinstance(v, list) else 0)
    return out


def engineer(df: pd.DataFrame) -> pd.DataFrame:
    """Raw snapshot columns (+ cell stats) -> model matrix. Pure function, safe to pickle."""
    out = pd.DataFrame(index=df.index)
    for c in ["stage", "elapsed_min", "dist_km", "rider_to_store_km", "rider_known",
              "idle_riders_near", "active_orders_in_store", "picker_queue_depth", "dow",
              "raining", "pick_minutes", "cell_slowness", "cell_count"]:
        out[c] = df[c].astype(float)
    out["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24.0)
    out["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24.0)
    out["travel_base_min"] = df["dist_km"] * ROAD_FACTOR / DEFAULT_SPEED_KMH * 60
    out["to_store_base_min"] = df["rider_to_store_km"] * ROAD_FACTOR / DEFAULT_SPEED_KMH * 60
    return out[FEATURES]
