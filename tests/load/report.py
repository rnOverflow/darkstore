"""Join Locust's client-side stage stats with server-side Prometheus numbers -> docs/load/results.{json,md}.

    uv run --no-sync python -m tests.load.report [--prom http://localhost:9090]

Every number in the table is read from this machine's run (stages.json written by the locustfile, and
Prometheus queried over each stage's own time window). Nothing is estimated.
"""
from __future__ import annotations

import argparse
import json
import platform
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "docs" / "load"
SETTLE = 20  # seconds ignored at the start of each stage (ramp-up of users)


def prom(base: str, expr: str, at: float) -> float | None:
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": f"{at:.3f}"})
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - local Prometheus
        res = json.load(r)["data"]["result"]
    if not res:
        return None
    v = float(res[0]["value"][1])
    return None if v != v else v  # NaN -> None


def fmt(v, nd=1, unit=""):
    return "-" if v is None else f"{v:.{nd}f}{unit}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", default="http://localhost:9090")
    base = ap.parse_args().prom
    data = json.loads((OUT / "stages.json").read_text())
    rows = []
    for s in data["stages"]:
        a, b = s["start"] + SETTLE, s["end"]
        w = int(b - a)
        q = lambda e: prom(base, e, b)  # noqa: E731
        inc = lambda m: f'sum(increase({m}[{w}s]))'  # noqa: E731
        cr, de = q(inc('darkstore_order_transitions_total{status="created"}')), q(inc('darkstore_order_transitions_total{status="delivered"}'))
        nr, asg = q(inc('darkstore_assignments_total{outcome="no_rider"}')), q(inc("darkstore_assignments_total"))
        rows.append({
            "stage": s["stage"], "seconds": s["seconds"], "client": s["client"],
            "server": {
                "created_per_s": None if cr is None else cr / w, "delivered_per_s": None if de is None else de / w,
                "assignment_wait_p95_s": q(f"histogram_quantile(0.95, sum by (le) (increase(darkstore_assignment_wait_seconds_bucket[{w}s])))"),
                "delivered_age_p50_s": q(f'histogram_quantile(0.5, sum by (le) (increase(darkstore_order_age_seconds_bucket{{status="delivered"}}[{w}s])))'),
                "delivered_age_p95_s": q(f'histogram_quantile(0.95, sum by (le) (increase(darkstore_order_age_seconds_bucket{{status="delivered"}}[{w}s])))'),
                "max_consumer_lag": q(f"max_over_time(max(darkstore_consumer_lag)[{w}s:5s])"),
                "no_rider_share": None if not asg or nr is None else nr / asg,
                "cancelled_or_rejected": q(inc('darkstore_order_transitions_total{status=~"cancelled|rejected"}')),
                "dead_lettered": q(inc('darkstore_events_total{outcome="dlq"}')),
                "ws_fanout_p99_ms": (lambda v: None if v is None else v * 1000)(q(f"histogram_quantile(0.99, sum by (le) (increase(darkstore_ws_fanout_seconds_bucket[{w}s])))")),
            }})
    result = {"machine": {"os": platform.platform(), "docker_vm": "8 CPU / ~4 GB (Docker Desktop)"},
              "trackers": data["trackers"], "stages": rows, "cpu_at_stage_end": data["cpu_at_stage_end"]}
    (OUT / "results.json").write_text(json.dumps(result, indent=2) + "\n")

    md = ["| Stage (target) | Orders created/s | Delivered/s | create p50 / p95 / p99 ms | rider wait p95 s | "
          "order→delivered p50 / p95 s (server) | max lag | no-rider share | cancelled | DLQ |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        c, sv = r["client"], r["server"]
        md.append(f"| {r['stage']} | {fmt(c['created_per_s'], 2)} (server {fmt(sv['created_per_s'], 2)}) | "
                  f"{fmt(sv['delivered_per_s'], 2)} | {fmt(c['create_ms']['p50'])} / {fmt(c['create_ms']['p95'])} / {fmt(c['create_ms']['p99'])} | "
                  f"{fmt(sv['assignment_wait_p95_s'])} | {fmt(sv['delivered_age_p50_s'], 0)} / {fmt(sv['delivered_age_p95_s'], 0)} | "
                  f"{fmt(sv['max_consumer_lag'], 0)} | {fmt(None if sv['no_rider_share'] is None else sv['no_rider_share'] * 100, 0, '%')} | "
                  f"{fmt(sv['cancelled_or_rejected'], 0)} | {fmt(sv['dead_lettered'], 0)} |")
    md += ["", f"WebSocket trackers ({data['trackers']} live sockets), client view:", "",
           "| Stage | snapshot latency p50 / p95 ms (n) | order→delivered p50 / p95 s (n) | WS failures | reconnects | hub→socket p99 ms |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        c, sv = r["client"], r["server"]
        md.append(f"| {r['stage']} | {fmt(c['ws_snapshot_ms']['p50'])} / {fmt(c['ws_snapshot_ms']['p95'])} ({c['ws_snapshot_ms']['n']}) | "
                  f"{fmt(c['tracked_to_delivered_s']['p50'], 0)} / {fmt(c['tracked_to_delivered_s']['p95'], 0)} ({c['tracked_to_delivered_s']['n']}) | "
                  f"{c['ws_failures']} | {c['ws_reconnects']} | {fmt(sv['ws_fanout_p99_ms'], 2)} |")
    md += ["", "Container CPU / memory sampled at the end of each stage (`docker stats`; CPU is % of one core):", ""]
    for s in data["cpu_at_stage_end"]:
        top = sorted(s["containers"].items(), key=lambda kv: -float(kv[1][0].rstrip("%") or 0))[:5]
        md.append(f"- {s['stage']}: " + ", ".join(f"{n} {c} ({m})" for n, (c, m) in top))
    (OUT / "results.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
