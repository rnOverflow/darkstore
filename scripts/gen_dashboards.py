#!/usr/bin/env python3
"""Dashboards as code: writes the five provisioned Grafana dashboards to infra/grafana/dashboards/.

    python scripts/gen_dashboards.py            # regenerate the JSON (commit the result)
    python scripts/gen_dashboards.py --check    # fail if the committed JSON is stale

Metric names are the contract in packages/common/src/darkstore_common/telemetry.py (Instruments).
scripts/check_dashboards.py runs every query below against a live Prometheus.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "infra" / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}
R = "$__rate_interval"


def q(expr: str, legend: str = "", ref: str = "A") -> dict:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}


def _targets(qs) -> list[dict]:
    return [q(*(x if isinstance(x, tuple) else (x,)), ref=chr(65 + i)) for i, x in enumerate(qs)]


class Board:
    def __init__(self, uid: str, title: str, description: str):
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self._x = self._y = self._row_h = 0
        self._id = 0

    def row(self, title: str) -> None:
        self._flush()
        self._id += 1
        self.panels.append({"id": self._id, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1}, "panels": []})
        self._y += 1

    def _flush(self) -> None:
        if self._x:
            self._y += self._row_h
            self._x = self._row_h = 0

    def _add(self, panel: dict, w: int, h: int) -> None:
        if self._x + w > 24:
            self._flush()
        self._id += 1
        panel.update(id=self._id, datasource=DS, gridPos={"x": self._x, "y": self._y, "w": w, "h": h})
        self.panels.append(panel)
        self._x += w
        self._row_h = max(self._row_h, h)

    def stat(self, title, expr, unit="short", w=4, thresholds=None, desc="", decimals=None) -> None:
        steps = thresholds or [{"color": "blue", "value": None}]
        defaults = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}
        if decimals is not None:
            defaults["decimals"] = decimals
        self._add({"type": "stat", "title": title, "description": desc, "targets": _targets([expr]),
                   "fieldConfig": {"defaults": defaults, "overrides": []},
                   "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value",
                               "graphMode": "area"}}, w, 4)

    def series(self, title, qs, unit="short", w=12, h=8, desc="", stack=False, minv=None) -> None:
        custom = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 12 if stack else 0,
                  "showPoints": "never", "spanNulls": False}
        if stack:
            custom["stacking"] = {"mode": "normal"}
        defaults = {"unit": unit, "custom": custom}
        if minv is not None:
            defaults["min"] = minv
        self._add({"type": "timeseries", "title": title, "description": desc, "targets": _targets(qs),
                   "fieldConfig": {"defaults": defaults, "overrides": []},
                   "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                               "tooltip": {"mode": "multi", "sort": "desc"}}}, w, h)

    def bars(self, title, expr, legend, unit="short", w=12, h=8, desc="") -> None:
        self._add({"type": "bargauge", "title": title, "description": desc,
                   "targets": [q(expr, legend)],
                   "fieldConfig": {"defaults": {"unit": unit, "min": 0}, "overrides": []},
                   "options": {"orientation": "horizontal", "displayMode": "gradient",
                               "reduceOptions": {"calcs": ["lastNotNull"]}}}, w, h)

    def heatmap(self, title, expr, w=12, h=8, desc="") -> None:
        t = q(expr, "{{le}}")
        t["format"] = "heatmap"
        self._add({"type": "heatmap", "title": title, "description": desc, "targets": [t],
                   "options": {"calculate": False, "yAxis": {"unit": "short"},
                               "color": {"scheme": "Oranges", "mode": "scheme"}}}, w, h)

    def json(self) -> dict:
        self._flush()
        return {"uid": self.uid, "title": self.title, "description": self.description,
                "tags": ["darkstore"], "timezone": "browser", "schemaVersion": 39, "version": 1,
                "editable": False, "refresh": "10s", "time": {"from": "now-30m", "to": "now"},
                "templating": {"list": []}, "annotations": {"list": []}, "panels": self.panels}


def quantiles(metric: str, by: str = "", qs=(0.5, 0.95, 0.99), flt: str = "") -> list[tuple[str, str]]:
    sel = f"{{{flt}}}" if flt else ""
    grp = f"le, {by}" if by else "le"
    legend = "p{{q}}" if not by else f"p{{{{q}}}} {{{{{by}}}}}"
    return [(f"histogram_quantile({p}, sum by ({grp}) (rate({metric}_bucket{sel}[{R}])))",
             legend.replace("{{q}}", str(int(p * 100)))) for p in qs]


# ---------------------------------------------------------------------------------------- boards

def order_funnel() -> Board:
    b = Board("darkstore-order-funnel", "1 · Order funnel & latency",
              "Where orders are in the saga and how long each stage takes. Counts are per window; "
              "latency is real seconds since the order was created.")
    created = 'sum(increase(darkstore_order_transitions_total{status="created"}[$__range]))'
    delivered = 'sum(increase(darkstore_order_transitions_total{status="delivered"}[$__range]))'
    lost = 'sum(increase(darkstore_order_transitions_total{status=~"cancelled|rejected"}[$__range]))'
    b.stat("Orders created", created)
    b.stat("Delivered", delivered, thresholds=[{"color": "green", "value": None}])
    b.stat("Cancelled / rejected", lost, thresholds=[{"color": "green", "value": None},
                                                       {"color": "orange", "value": 1}])
    b.stat("Delivered share of created", f"({delivered} or vector(0)) / {created}", unit="percentunit", w=6,
           thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.5},
                       {"color": "green", "value": 0.9}],
           desc="Includes orders still in flight, so it dips while the window is young.")
    b.stat("p95 order → delivered",
           'histogram_quantile(0.95, sum by (le) (rate(darkstore_order_age_seconds_bucket{status="delivered"}[$__range])))',
           unit="s", w=6)
    b.row("Funnel")
    b.bars("Orders reaching each status (window)",
           'sum by (status) (increase(darkstore_order_transitions_total[$__range]))', "{{status}}",
           desc="created → reserved → picking → picked → assigned → out_for_delivery → delivered; "
                "cancelled/rejected branch off.")
    b.series("Status transitions per minute",
             [(f"sum by (status) (rate(darkstore_order_transitions_total[{R}])) * 60", "{{status}}")],
             unit="opm", stack=True)
    b.row("Latency")
    b.series("End-to-end: order created → delivered",
             quantiles("darkstore_order_age_seconds", flt='status="delivered"'), unit="s",
             desc="Real seconds (the simulation runs 30× faster than the clock).")
    b.series("Time to reach each stage (p95)",
             [(f'histogram_quantile(0.95, sum by (le, status) (rate(darkstore_order_age_seconds_bucket[{R}])))',
               "{{status}}")], unit="s")
    return b


def kafka() -> Board:
    b = Board("darkstore-kafka", "2 · Kafka consumer lag & DLQ",
              "Consumer health per group: backlog, throughput, retries and the dead-letter queue.")
    b.stat("Max consumer lag", "max(darkstore_consumer_lag)", unit="short", thresholds=[
        {"color": "green", "value": None}, {"color": "orange", "value": 100}, {"color": "red", "value": 1000}])
    b.stat("Dead-lettered (window)",
           'sum(increase(darkstore_events_total{outcome="dlq"}[$__range]))', thresholds=[
               {"color": "green", "value": None}, {"color": "red", "value": 1}],
           desc="Anything above 0 needs a human: scripts/replay.py after fixing the cause.")
    b.stat("Retries (window)", 'sum(increase(darkstore_events_total{outcome="retry"}[$__range]))',
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 50}])
    b.stat("Duplicates dropped (window)",
           'sum(increase(darkstore_events_total{outcome="duplicate"}[$__range]))',
           desc="At-least-once delivery working as designed: applied once, dropped here.")
    b.stat("Redpanda up", 'min(up{job="redpanda"})', thresholds=[
        {"color": "red", "value": None}, {"color": "green", "value": 1}], w=4)
    b.stat("Consumers reporting", "count(count by (group) (darkstore_consumer_lag))", w=4)
    b.row("Lag")
    b.series("Consumer lag by group", [("sum by (group) (darkstore_consumer_lag)", "{{group}}")],
             minv=0, desc="Log end − committed position, retry topics included.")
    b.series("Throughput by group (events/s)",
             [(f'sum by (group) (rate(darkstore_events_total{{outcome="ok"}}[{R}]))', "{{group}}")],
             unit="ops")
    b.row("Failure handling")
    b.series("Retry / DLQ / duplicate rate",
             [(f'sum by (outcome) (rate(darkstore_events_total{{outcome!="ok"}}[{R}]))', "{{outcome}}")],
             unit="ops", stack=True)
    b.series("DLQ by group and event type (window)",
             [('sum by (group, event_type) (increase(darkstore_events_total{outcome="dlq"}[$__range]))',
               "{{group}} · {{event_type}}")])
    b.row("Latency")
    b.series("Pipeline latency: event occurred → consumer starts (p95)",
             quantiles("darkstore_event_age_seconds", by="group", qs=(0.95,)), unit="s",
             desc="Includes outbox relay, broker and queueing. Retried events show their full age.")
    b.series("Handler duration (p95)",
             quantiles("darkstore_event_handle_seconds", by="group", qs=(0.95,)), unit="s")
    return b


def matching() -> Board:
    b = Board("darkstore-matching", "3 · Rider matching & assignment",
              "How quickly a ready order gets a rider, and how often a search comes back empty.")
    ok = 'sum(increase(darkstore_assignments_total{outcome="assigned"}[$__range]))'
    miss = 'sum(increase(darkstore_assignments_total{outcome="no_rider"}[$__range]))'
    b.stat("Assigned (window)", ok, thresholds=[{"color": "green", "value": None}])
    b.stat("Search attempts that found a rider", f"({ok} or vector(0)) / (({ok} or vector(0)) + ({miss} or vector(0)))", unit="percentunit", w=6,
           thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.3},
                       {"color": "green", "value": 0.7}],
           desc="Each pending order is retried every loop tick, so a saturated fleet shows a low ratio.")
    b.stat("Pickup timeouts (window)",
           'sum(increase(darkstore_assignments_total{outcome="timed_out"}[$__range]))',
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}])
    b.stat("p95 wait for a rider",
           "histogram_quantile(0.95, sum by (le) (rate(darkstore_assignment_wait_seconds_bucket[$__range])))",
           unit="s", w=6)
    b.row("Latency")
    b.series("Wait for a rider: picked → assigned", quantiles("darkstore_assignment_wait_seconds"),
             unit="s", desc="From the order being ready for dispatch until a rider is claimed.")
    b.series("Matching search duration (H3 k-ring + score + atomic claim)",
             quantiles("darkstore_match_duration_seconds"), unit="s")
    b.row("Outcomes")
    b.series("Dispatch outcomes per second",
             [(f"sum by (outcome) (rate(darkstore_assignments_total[{R}]))", "{{outcome}}")],
             unit="ops", stack=True)
    b.heatmap("Wait-for-a-rider distribution",
              f"sum by (le) (increase(darkstore_assignment_wait_seconds_bucket[{R}]))")
    return b


def eta() -> Board:
    b = Board("darkstore-eta", "4 · ETA accuracy & drift",
              "The first ETA promised to the customer vs the actual delivery time (simulated minutes). "
              "Watch coverage and bias for drift; fallback share shows when the model is not serving.")
    b.stat("MAE (window)",
           "sum(rate(darkstore_eta_abs_error_minutes_sum[$__range])) / sum(rate(darkstore_eta_abs_error_minutes_count[$__range]))",
           unit="m", decimals=2, desc="Mean |first P50 − actual|, simulated minutes.")
    b.stat("P90 coverage (window)",
           'sum(increase(darkstore_eta_outcomes_total{covered="true"}[$__range])) / sum(increase(darkstore_eta_outcomes_total[$__range]))',
           unit="percentunit", w=5, decimals=1, thresholds=[
               {"color": "red", "value": None}, {"color": "orange", "value": 0.8},
               {"color": "green", "value": 0.85}], desc="Target 90%: share delivered at or before the promised P90.")
    b.stat("Bias (window)",
           "((sum(increase(darkstore_eta_late_minutes_total[$__range])) or vector(0)) - (sum(increase(darkstore_eta_early_minutes_total[$__range])) or vector(0))) / sum(increase(darkstore_eta_outcomes_total[$__range]))",
           unit="m", decimals=2, desc="Negative = promised too early (we under-estimate).")
    b.stat("Fallback share",
           '(sum(increase(darkstore_eta_predictions_total{source!="model"}[$__range])) or vector(0)) / sum(increase(darkstore_eta_predictions_total[$__range]))',
           unit="percentunit", w=5, thresholds=[{"color": "green", "value": None},
                                                  {"color": "orange", "value": 0.05}],
           desc="Predictions served by the heuristic instead of the model.")
    b.stat("Delivered orders scored", "sum(increase(darkstore_eta_outcomes_total[$__range]))", w=4)
    b.row("Accuracy over time")
    b.series("MAE: now vs 1 h earlier (drift)",
             [("sum(rate(darkstore_eta_abs_error_minutes_sum[15m])) / sum(rate(darkstore_eta_abs_error_minutes_count[15m]))", "now"),
              ("sum(rate(darkstore_eta_abs_error_minutes_sum[15m] offset 1h)) / sum(rate(darkstore_eta_abs_error_minutes_count[15m] offset 1h))", "1 h earlier")],
             unit="m", minv=0, desc="Windows are 15 minutes so low-volume demos are noisy; a persistent gap is drift.")
    b.series("P90 coverage (15 min windows)",
             [('sum(increase(darkstore_eta_outcomes_total{covered="true"}[15m])) / sum(increase(darkstore_eta_outcomes_total[15m]))', "coverage"),
              ("vector(0.9)", "target")], unit="percentunit", minv=0)
    b.series("Bias (15 min windows)",
             [("((sum(increase(darkstore_eta_late_minutes_total[15m])) or vector(0)) - (sum(increase(darkstore_eta_early_minutes_total[15m])) or vector(0))) / sum(increase(darkstore_eta_outcomes_total[15m]))", "p50 − actual")],
             unit="m")
    b.series("MAE by ETA source",
             [("sum by (source) (rate(darkstore_eta_abs_error_minutes_sum[15m])) / sum by (source) (rate(darkstore_eta_abs_error_minutes_count[15m]))", "{{source}}")],
             unit="m", minv=0, desc="model vs heuristic on the same delivered orders, when both served.")
    b.row("Serving")
    b.series("Predictions per second by source",
             [(f"sum by (source) (rate(darkstore_eta_predictions_total[{R}]))", "{{source}}")],
             unit="ops", stack=True)
    b.heatmap("Absolute error distribution (minutes)",
              "sum by (le) (increase(darkstore_eta_abs_error_minutes_bucket[15m]))")
    return b


def websocket() -> Board:
    b = Board("darkstore-websocket", "5 · WebSocket connections & fan-out",
              "Tracking gateway health: open sockets per replica, how long events wait before reaching "
              "a socket, and clients that were slow or cut off.")
    b.stat("Open connections", "sum(darkstore_ws_connections)", w=5)
    b.stat("Gateway replicas reporting", "count(count by (service_instance_id) (darkstore_ws_connections))", w=5)
    b.stat("p99 fan-out wait",
           "histogram_quantile(0.99, sum by (le) (rate(darkstore_ws_fanout_seconds_bucket[$__range])))",
           unit="s", w=5, decimals=3)
    b.stat("Slow-client drops (window)", "sum(increase(darkstore_ws_dropped_total[$__range])) or vector(0)", w=5,
           desc="Position/ETA updates discarded for clients that read slowly. Status updates are never dropped.")
    b.stat("Clients cut off (window)", "sum(increase(darkstore_ws_overflow_total[$__range])) or vector(0)", w=4,
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}])
    b.row("Connections")
    b.series("Connections by gateway replica",
             [("sum by (service_instance_id) (darkstore_ws_connections)", "{{service_instance_id}}")],
             stack=True, minv=0)
    b.series("Connections by role", [("sum by (role) (darkstore_ws_connections)", "{{role}}")], minv=0)
    b.row("Fan-out latency")
    b.series("Hub → socket wait", quantiles("darkstore_ws_fanout_seconds"), unit="s",
             desc="Time an event sits in the per-connection queue before it is written to the socket.")
    b.series("Kafka → gateway ingest lag (p95)",
             quantiles("darkstore_event_age_seconds", by="event_type", qs=(0.95,), flt='group="gateway"'),
             unit="s", desc="Event occurred → gateway consumer starts on it, per event type.")
    b.row("Backpressure")
    b.series("Dropped / cut off per second",
             [(f"sum(rate(darkstore_ws_dropped_total[{R}])) or vector(0)", "dropped (position/eta)"),
              (f"sum(rate(darkstore_ws_overflow_total[{R}])) or vector(0)", "connections cut")], unit="ops")
    return b


BOARDS = {"order-funnel": order_funnel, "kafka": kafka, "matching": matching, "eta": eta,
          "websocket": websocket}


def render() -> dict[str, str]:
    return {f"{name}.json": json.dumps(fn().json(), indent=2) + "\n" for name, fn in BOARDS.items()}


def all_queries() -> list[tuple[str, str, str]]:
    """(dashboard, panel title, expr) for scripts/check_dashboards.py."""
    out = []
    for fn in BOARDS.values():
        d = fn().json()
        for p in d["panels"]:
            for t in p.get("targets", []):
                out.append((d["title"], p["title"], t["expr"]))
    return out


if __name__ == "__main__":
    files = render()
    if "--check" in sys.argv:
        stale = [n for n, body in files.items() if not (OUT / n).exists() or (OUT / n).read_text() != body]
        if stale:
            sys.exit(f"stale dashboards (run scripts/gen_dashboards.py): {stale}")
        print(f"{len(files)} dashboards up to date")
    else:
        OUT.mkdir(parents=True, exist_ok=True)
        for n, body in files.items():
            (OUT / n).write_text(body)
        print(f"wrote {len(files)} dashboards to {OUT}")
