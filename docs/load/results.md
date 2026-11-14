| Stage (target) | Orders created/s | Delivered/s | create p50 / p95 / p99 ms | rider wait p95 s | order→delivered p50 / p95 s (server) | max lag | no-rider share | cancelled | DLQ |
|---|---|---|---|---|---|---|---|---|---|
| 0.5/s | 1.01 (server 0.90) | 0.90 | 36.5 / 152.8 / 180.0 | 0.3 | 23 / 53 | 4 | - | - | 0 |
| 1/s | 1.42 (server 1.45) | 1.42 | 24.6 / 139.3 / 332.0 | 0.4 | 25 / 55 | 6 | - | - | 0 |
| 1.5/s | 1.87 (server 1.92) | 1.80 | 36.3 / 225.5 / 471.4 | 0.4 | 24 / 55 | 4 | - | - | 0 |
| 2/s | 2.28 (server 2.08) | 1.95 | 35.4 / 275.8 / 480.3 | 0.4 | 31 / 57 | 7 | - | - | 0 |
| 3/s | 3.19 (server 3.15) | 2.80 | 61.7 / 336.4 / 753.7 | 0.8 | 35 / 58 | 28 | - | - | 0 |
| 4/s | 4.08 (server 4.13) | 3.27 | 78.0 / 365.2 / 514.6 | 0.7 | 44 / 91 | 168 | - | - | 0 |

WebSocket trackers (12 live sockets), client view:

| Stage | snapshot latency p50 / p95 ms (n) | order→delivered p50 / p95 s (n) | WS failures | reconnects | hub→socket p99 ms |
|---|---|---|---|---|---|
| 0.5/s | 27.3 / 109.5 (30) | 30 / 41 (18) | 0 | 0 | 17.56 |
| 1/s | 23.0 / 173.1 (39) | 24 / 44 (40) | 0 | 0 | 22.42 |
| 1.5/s | 22.7 / 136.9 (39) | 28 / 46 (38) | 0 | 0 | 9.74 |
| 2/s | 26.5 / 140.1 (33) | 31 / 41 (33) | 0 | 0 | 18.74 |
| 3/s | 29.8 / 151.4 (32) | 33 / 46 (32) | 0 | 0 | 24.12 |
| 4/s | 26.8 / 142.6 (23) | 39 / 62 (23) | 0 | 0 | 26.50 |

Container CPU / memory sampled at the end of each stage (`docker stats`; CPU is % of one core):

- 0.5/s: eta 48.02% (154.7MiB), rider-sim 37.26% (64.84MiB), order 34.18% (80.11MiB), postgres 33.11% (102.9MiB), dispatch 32.63% (71.12MiB)
- 1/s: rider-sim 28.20% (64.15MiB), postgres 24.88% (114.7MiB), grafana 22.82% (90.31MiB), redpanda 20.96% (265.7MiB), gateway-2 20.89% (84.12MiB)
- 1.5/s: eta 61.94% (154.8MiB), dispatch 38.84% (72.04MiB), order 33.42% (81MiB), rider-sim 33.03% (64.52MiB), redpanda 32.83% (267.1MiB)
- 2/s: eta 58.30% (155.5MiB), postgres 42.86% (122MiB), dispatch 39.34% (72.23MiB), order 35.53% (81.96MiB), rider-sim 32.08% (63.8MiB)
- 3/s: ops-api 64.40% (48.44MiB), redpanda 55.75% (274.5MiB), eta 54.53% (156.4MiB), dispatch 48.57% (72.88MiB), rider-sim 40.77% (63.8MiB)
- 4/s: gateway-1 83.46% (88.78MiB), order 80.97% (95.32MiB), gateway-2 60.20% (96.38MiB), dispatch 55.48% (73.54MiB), postgres 49.29% (135.5MiB)
