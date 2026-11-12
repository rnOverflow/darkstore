"""Offline trip generator: realistic deliveries -> Parquet-ready snapshots.

Each simulated order yields FOUR snapshots, one per state the live system re-predicts at
(created, picking, rider assigned, out for delivery). A snapshot holds only what is knowable at
that moment; the target is the minutes still remaining. The world's physics (traffic, rain, slow
H3 cells, picker queues) comes from ``darkstore_common.world``, the same code the live rider-sim
uses, so the model is trained on the effects the running system produces.
"""
from __future__ import annotations

import math
import random

import numpy as np
import pandas as pd

from darkstore_common.city import STORES, nearest_store, random_point
from darkstore_common.geo import ROAD_FACTOR, cell_of, haversine_km
from darkstore_common.world import PICKERS_PER_STORE, is_raining, pick_minutes, speed_multiplier
from darkstore_simulator.demand import orders_per_minute

STAGES = ["created", "picking", "assigned", "out_for_delivery"]
TRIPS_PER_DAY = 150


def generate_trips(n_trips: int, seed: int = 0, trips_per_day: int = TRIPS_PER_DAY) -> pd.DataFrame:
    rng, nrng = random.Random(seed), np.random.default_rng(seed)
    days = max(7, math.ceil(n_trips / trips_per_day))
    hour_weights = [orders_per_minute(h + 0.5, 1.0) for h in range(24)]
    trips = []
    for i in range(n_trips):
        day = rng.randrange(days)
        hour = rng.choices(range(24), weights=hour_weights)[0] + rng.random()
        trips.append((day * 86400.0 + hour * 3600.0, day, hour, i))
    trips.sort()  # time order: the train/val/test split is chronological

    rows = []
    for order_ts, day, hour, i in trips:
        raining, dow = is_raining(day), day % 7
        # same process as the live order simulator: a customer anywhere in the city, served by the
        # nearest dark store (so dist_km has the same distribution live and offline)
        clat, clng = random_point(rng)
        store_id = nearest_store(clat, clng)
        slat, slng = STORES[store_id]
        dist_km = haversine_km(slat, slng, clat, clng)
        cust_cell, store_cell = cell_of(clat, clng), cell_of(slat, slng)

        # store and fleet state at order time
        active = int(nrng.poisson(max(0.5, orders_per_minute(hour, 0.6) * 10)))
        in_pick = int(nrng.binomial(active, 0.35)) if active else 0
        queue = max(0, in_pick - PICKERS_PER_STORE)
        util = min(1.0, active / 12.0)
        idle_near = int(nrng.poisson(max(0.2, 6.0 * (1 - util))))
        est_rider_km = rng.expovariate(1 / (1.5 / (1 + idle_near) ** 0.8)) if idle_near else 2.5
        est_rider_km = min(est_rider_km, 8.0)

        # what actually happens
        queue_wait = 0.0   # picking starts as soon as stock is reserved; queues only slow the pick itself
        pick = pick_minutes(rng, queue, bool(raining))
        rider_wait = rng.expovariate(1 / 3.0) if idle_near == 0 else 0.0
        rider_km = max(0.1, est_rider_km * rng.uniform(0.85, 1.15))
        speed = min(28.0, max(12.0, rng.gauss(20.0, 2.5)))
        to_store = rider_km * ROAD_FACTOR / (speed * speed_multiplier(hour, bool(raining), store_cell)) * 60
        to_cust = dist_km * ROAD_FACTOR / (speed * speed_multiplier(hour, bool(raining), cust_cell)) * 60
        to_cust *= max(0.7, rng.gauss(1.0, 0.08))
        t_assigned = queue_wait + pick + rider_wait
        t_pickup = t_assigned + to_store
        total = t_pickup + to_cust

        base = dict(order_id=f"t{seed}-{i}", order_ts=order_ts, dist_km=dist_km,
                    idle_riders_near=idle_near, active_orders_in_store=active,
                    picker_queue_depth=queue, hour=hour, dow=dow, raining=int(raining),
                    customer_cell=cust_cell, store_id=store_id)
        for stage, elapsed in enumerate([0.0, queue_wait, t_assigned, t_pickup]):
            known = stage >= 2
            rows.append({**base, "stage": stage, "elapsed_min": elapsed,
                         "rider_known": int(known),
                         "rider_to_store_km": rider_km if known else est_rider_km,
                         "pick_minutes": pick if known else np.nan,
                         "target_min": total - elapsed})
    return pd.DataFrame(rows)
