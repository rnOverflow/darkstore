# ETA model report: 20261113071018-s42

Held-out test = the last 15% of trips by order time (6000 trips, 24000 state snapshots; days 227-267). Train 28000 / val 6000 trips. All numbers are on SIMULATED trips (see docs/ml/README.md).

*Heuristic* = distance / 20 km/h x 1.4 road factor + 3 min pick, P90 = x1.35 (the production fallback). *Calibrated heuristic* = the same formula with two scale factors fitted on the training period per (stage, raining, rush-hour band): what a careful engineer would build without ML.

| | Heuristic | Calibrated heuristic | LightGBM | vs calibrated |
|---|---|---|---|---|
| MAE (min) | 12.00 | 4.69 | 3.98 | -15% |
| MAPE | 31.0% | 14.9% | 12.5% | -16% |
| RMSE (min) | 16.98 | 6.74 | 5.84 | -13% |
| P90 pinball loss | 5.77 | 1.19 | 1.04 | -13% |
| P90 coverage (target 90%) | 41.1% | 90.1% | 89.9% | |
| P90 mean width over P50 (min) | 7.41 | 8.73 | 6.91 | |

95% CI by trip bootstrap: LightGBM MAE 3.88-4.08, calibrated heuristic 4.59-4.81, improvement 14% to 17%.

Uncalibrated LightGBM P90 coverage: 88.5%; conformal offset +0.37 min.

**By stage (MAE minutes)**

| Stage | Heuristic | Calibrated heuristic | LightGBM | LightGBM P90 coverage |
|---|---|---|---|---|
| created | 14.23 | 5.41 | 4.55 | 89.5% |
| picking | 14.23 | 5.41 | 4.55 | 89.5% |
| assigned | 11.25 | 4.38 | 3.93 | 89.8% |
| out_for_delivery | 8.28 | 3.57 | 2.88 | 90.7% |

Top features (gain): dist_km, rider_to_store_km, stage, hour_sin, hour_cos, cell_slowness, raining, to_store_base_min
