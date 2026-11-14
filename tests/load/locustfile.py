"""Locust ramp against the front door (nginx :8088): order creation + live WebSocket trackers.

    make load            # headless, writes docs/load/{stages.json,results.md}

Two user classes:
  * OrderUser: places an order every ~2 s (so N users ~ N/2 orders/s), the arrival-rate dial.
  * Tracker (fixed 12): places an order, opens the tracking WebSocket like the real client (auth,
    subscribe, ack, pong) and follows it to `delivered`: measures snapshot latency and client-observed
    order -> delivered time. 12 because nginx allows 20 sockets per client IP; this machine has one IP.
The StageShape drives the ramp and records, per stage, what the CLIENT saw. Server-side numbers
(delivery rate, assignment wait, lag) come from Prometheus in tests/load/report.py.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import time
import uuid
from pathlib import Path

import websocket  # websocket-client: blocking, plays well with gevent
from darkstore_common.city import STORES
from locust import HttpUser, LoadTestShape, constant, events, task

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "load"
TRACKERS = 12
# (name, order users, seconds): ~ order users / 2 orders per second
STAGES = [("0.5/s", 1, 60), ("1/s", 2, 90), ("1.5/s", 3, 90), ("2/s", 4, 90), ("3/s", 6, 90), ("4/s", 8, 90)]
if os.environ.get("LOAD_STAGES"):   # e.g. LOAD_STAGES='[["0.5/s",1,120]]' for a short probe
    STAGES = [tuple(s) for s in json.loads(os.environ["LOAD_STAGES"])]

STATE = {"stage": 0, "stats": [{"create_ms": [], "create_fail": 0, "snap_ms": [], "e2e_s": [], "ws_fail": 0,
                                  "reconnects": 0} for _ in STAGES]}


def session(user: HttpUser) -> None:
    """Guest session: the server assigns the customer id; the token can only create orders."""
    with user.client.post("/api/sessions", name="POST /api/sessions", catch_response=True) as r:
        if r.status_code == 201:
            user.guest = r.json()["token"]
            r.success()
        else:
            user.guest = ""
            r.failure(f"HTTP {r.status_code}")


def payload() -> dict:
    s = random.randrange(6)
    lat0, lng0 = STORES[f"store-{s}"]   # the destination must be inside the store's delivery area
    return {"store_id": f"store-{s}",
            "lat": lat0 + random.uniform(-0.01, 0.01), "lng": lng0 + random.uniform(-0.03, 0.03),
            "items": [{"sku": f"sku-{random.randrange(20)}", "qty": random.randint(1, 2)}
                      for _ in range(random.randint(1, 3))]}


def create(user: HttpUser):
    st = STATE["stats"][STATE["stage"]]
    t0 = time.perf_counter()
    if not getattr(user, "guest", ""):
        session(user)
    with user.client.post("/api/orders", json=payload(), headers={
            "Idempotency-Key": uuid.uuid4().hex, "Authorization": f"Bearer {getattr(user, 'guest', '')}"},
                          name="POST /api/orders", catch_response=True) as r:
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code == 201:
            st["create_ms"].append(ms)
            r.success()
            return r.json()
        st["create_fail"] += 1
        if r.status_code == 401:
            user.guest = ""        # expired: get a new session next time
        r.failure(f"HTTP {r.status_code}")
    return None


class OrderUser(HttpUser):
    wait_time = constant(2)

    @task
    def place(self):
        create(self)


class Tracker(HttpUser):
    fixed_count = TRACKERS
    wait_time = constant(1)

    @task
    def follow(self):
        st = STATE["stats"][STATE["stage"]]
        t_order = time.perf_counter()
        j = create(self)
        if not j:
            return
        deadline, last_id, status = time.time() + 420, None, None
        first_connect = True
        while time.time() < deadline and status != "delivered":
            try:
                t0 = time.perf_counter()
                ws = websocket.create_connection(self.host.replace("http", "ws") + "/ws", timeout=20,
                                                 origin="http://localhost:8088")
                ws.send(json.dumps({"type": "auth", "token": j["tracking_token"]}))
                last_ack = 0.0
                while status != "delivered":
                    m = json.loads(ws.recv())
                    k = m.get("type")
                    if k == "auth_ok":
                        ws.send(json.dumps({"type": "subscribe", "order_id": j["order_id"], "last_event_id": last_id}))
                    elif k == "snapshot":
                        if first_connect:
                            first_connect = False
                            ms = (time.perf_counter() - t0) * 1000
                            st["snap_ms"].append(ms)
                            events.request.fire(request_type="WS", name="connect+auth+snapshot",
                                                response_time=ms, response_length=0, exception=None, context={})
                        last_id, status = m.get("last_event_id") or last_id, m["status"]
                    elif k == "event":
                        last_id = m["id"]
                        if m["kind"] == "status":
                            status = m["data"]["status"]
                    elif k == "heartbeat":
                        ws.send(json.dumps({"type": "pong"}))
                    if k in ("snapshot", "event") and time.time() - last_ack > 1:
                        last_ack = time.time()
                        ws.send(json.dumps({"type": "ack"}))
                    if status in ("cancelled", "rejected"):
                        break
                ws.close()
            except Exception:  # noqa: BLE001 - drop: resume like the real client
                STATE["stats"][STATE["stage"]]["reconnects"] += 1
                time.sleep(1)
        if status == "delivered":
            e2e = time.perf_counter() - t_order
            STATE["stats"][STATE["stage"]]["e2e_s"].append(e2e)
            events.request.fire(request_type="WS", name="order -> delivered (client view)",
                                response_time=e2e * 1000, response_length=0, exception=None, context={})
        else:
            STATE["stats"][STATE["stage"]]["ws_fail"] += 1
            events.request.fire(request_type="WS", name="order -> delivered (client view)", response_time=0,
                                response_length=0, exception=RuntimeError(f"ended as {status}"), context={})


class StageShape(LoadTestShape):
    """Step ramp. Records stage boundaries and a `docker stats` sample at each stage's end."""

    def __init__(self):
        super().__init__()
        self.t0 = None
        self.marks: list[dict] = []
        self.cpu: list[dict] = []

    def tick(self):
        now = time.time()
        if self.t0 is None:
            self.t0 = now
        el, acc = now - self.t0, 0
        for i, (name, users, secs) in enumerate(STAGES):
            if el < acc + secs:
                if i != STATE["stage"] or not self.marks:
                    if self.marks:
                        self.cpu.append(self._stats(STAGES[STATE["stage"]][0]))
                    STATE["stage"] = i
                    self.marks.append({"stage": name, "order_users": users, "start": now})
                    if len(self.marks) > 1:
                        self.marks[-2]["end"] = now
                return TRACKERS + users, 10
            acc += secs
        self.marks[-1]["end"] = now
        self.cpu.append(self._stats(STAGES[-1][0]))
        return None

    @staticmethod
    def _stats(stage: str) -> dict:
        try:
            out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
                                 capture_output=True, text=True, timeout=30).stdout
            rows = [r.split("|") for r in out.splitlines() if r.startswith("darkstore-")]
            return {"stage": stage, "containers": {r[0].removeprefix("darkstore-").removesuffix("-1"): [r[1], r[2].split(" / ")[0]] for r in rows}}
        except Exception:  # noqa: BLE001
            return {"stage": stage, "containers": {}}


def pct(xs: list[float], p: float):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else None


@events.quitting.add_listener
def dump(environment, **_):
    shape = environment.shape_class
    stages = []
    for m, (name, users, secs), s in zip(shape.marks, STAGES, STATE["stats"]):
        dur = (m.get("end") or time.time()) - m["start"]
        stages.append({**m, "seconds": round(dur, 1), "order_users": users,
                       "client": {"orders_created": len(s["create_ms"]), "create_failures": s["create_fail"],
                                  "created_per_s": round(len(s["create_ms"]) / dur, 2),
                                  "create_ms": {"p50": pct(s["create_ms"], .5), "p95": pct(s["create_ms"], .95),
                                                "p99": pct(s["create_ms"], .99)},
                                  "ws_snapshot_ms": {"p50": pct(s["snap_ms"], .5), "p95": pct(s["snap_ms"], .95), "n": len(s["snap_ms"])},
                                  "tracked_to_delivered_s": {"p50": pct(s["e2e_s"], .5), "p95": pct(s["e2e_s"], .95), "n": len(s["e2e_s"])},
                                  "ws_failures": s["ws_fail"], "ws_reconnects": s["reconnects"]}})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stages.json").write_text(json.dumps({"stages": stages, "cpu_at_stage_end": shape.cpu,
                                                 "trackers": TRACKERS}, indent=2) + "\n")
