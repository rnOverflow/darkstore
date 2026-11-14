"""Chaos runner: place orders, break something mid-flow, prove nothing was lost or duplicated.

    uv run --no-sync python -m tests.chaos.run <scenario> [--orders N] [--rate R]
    scenarios: kill-picking kill-dispatch kill-order kill-gateway broker-restart broker-crash

Needs the compose stack up (`make up`) and quiet. Talks to the order API on 127.0.0.1:8001, Postgres on
127.0.0.1:5433, the WebSocket via nginx on 127.0.0.1:8088, and the `docker compose` CLI to inject faults.
The client behaves like a real one: it retries a failed POST with the SAME Idempotency-Key, and every
request is also sent twice at once (a double-click / network retry). Results go to docs/chaos/<scenario>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import asyncpg
import httpx
import websockets
from darkstore_common.city import STORES

from .invariants import RANK, TERMINAL, Evidence, WsResult, violations

ROOT = Path(__file__).resolve().parents[2]
ORDER_API = "http://127.0.0.1:8001"
WS_URL = "ws://127.0.0.1:8088/ws"
ORIGIN = "http://localhost:8088"
OUTAGE_S = 15

SCENARIOS = {
    "kill-picking": ("kill", "picking"), "kill-dispatch": ("kill", "dispatch"),
    "kill-order": ("kill", "order"), "kill-gateway": ("kill", "gateway-1"),
    "broker-restart": ("restart", "redpanda"), "broker-crash": ("kill", "redpanda"),
}


def env_file() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def compose(*args: str, timeout: int = 180) -> str:
    try:
        r = subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "i/o timeout (docker compose call timed out)"
    return (r.stdout + r.stderr).strip()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------- client

class Client:
    def __init__(self, run_id: str, rng: random.Random):
        self.run_id, self.rng = run_id, rng
        self.attempts: dict[str, set[str]] = {}
        self.tokens: dict[str, str] = {}
        self.latencies: list[float] = []
        self.failed_attempts = 0
        self.gave_up: list[str] = []
        self.session: dict[str, str] = {}

    async def open_session(self, http: httpx.AsyncClient) -> None:
        r = await http.post("/sessions")
        r.raise_for_status()
        self.session = r.json()

    def payload(self) -> dict:
        s = self.rng.randrange(6)
        lat0, lng0 = STORES[f"store-{s}"]   # the destination must be inside the store's delivery area
        return {"store_id": f"store-{s}",
                "lat": lat0 + self.rng.uniform(-0.01, 0.01), "lng": lng0 + self.rng.uniform(-0.03, 0.03),
                "items": [{"sku": f"sku-{self.rng.randrange(20)}", "qty": self.rng.randint(1, 2)}
                          for _ in range(self.rng.randint(1, 3))]}

    async def _post(self, http: httpx.AsyncClient, key: str, body: dict) -> None:
        t0 = time.perf_counter()
        try:
            r = await http.post("/orders", json=body, headers={
                "Idempotency-Key": key, "Authorization": f"Bearer {self.session['token']}"})
        except httpx.HTTPError:
            self.failed_attempts += 1
            raise
        if r.status_code not in (200, 201):
            self.failed_attempts += 1
            raise RuntimeError(f"HTTP {r.status_code}")
        self.latencies.append(time.perf_counter() - t0)
        j = r.json()
        self.attempts[key].add(j["order_id"])
        if "tracking_token" in j:
            self.tokens[j["order_id"]] = j["tracking_token"]

    async def place(self, http: httpx.AsyncClient, i: int) -> None:
        key, body = f"{self.run_id}-{i}", self.payload()
        self.attempts[key] = set()
        deadline = time.time() + 150
        delay = 0.5
        while time.time() < deadline:
            # the same request twice at once: a double click / aggressive client retry
            res = await asyncio.gather(self._post(http, key, body), self._post(http, key, body),
                                       return_exceptions=True)
            if any(not isinstance(r, Exception) for r in res):
                return
            await asyncio.sleep(delay)
            delay = min(delay * 1.7, 5)
        self.gave_up.append(key)


# ---------------------------------------------------------------------------------------------- watchers

async def watch(order_id: str, token: str, result: WsResult, deadline: float) -> None:
    """A tracking client that follows the documented protocol: reconnect with backoff, resume by event id."""
    last_id, backoff, first = None, 0.5, True
    while time.time() < deadline and not result.reached_delivered:
        if not first:
            result.reconnects += 1
        first = False
        try:
            async with websockets.connect(WS_URL, additional_headers={"Origin": ORIGIN}, open_timeout=10,
                                          ping_interval=None) as ws:
                await ws.send(json.dumps({"type": "auth", "token": token}))
                last_ack = 0.0
                async for raw in ws:
                    m = json.loads(raw)
                    t = m.get("type")
                    if t == "auth_ok":
                        await ws.send(json.dumps({"type": "subscribe", "order_id": order_id,
                                                  "last_event_id": last_id}))
                        backoff = 0.5
                    elif t == "snapshot":
                        last_id = m.get("last_event_id") or last_id
                        result.statuses.append(m["status"])
                    elif t == "event":
                        last_id = m["id"]
                        if m["kind"] == "status":
                            result.statuses.append(m["data"]["status"])
                    elif t == "heartbeat":
                        await ws.send(json.dumps({"type": "pong"}))
                    if t in ("snapshot", "event") and time.time() - last_ack > 1:
                        last_ack = time.time()
                        await ws.send(json.dumps({"type": "ack"}))
                    if result.statuses and result.statuses[-1] in TERMINAL:
                        result.reached_delivered = result.statuses[-1] == "delivered"
                        return
        except Exception:  # noqa: BLE001 - any drop: back off and resume, like the real client
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5)


# ---------------------------------------------------------------------------------------------- db

class Db:
    def __init__(self, pool: asyncpg.Pool):
        self.p = pool

    @classmethod
    async def connect(cls) -> "Db":
        e = env_file()
        dsn = f"postgresql://{e.get('POSTGRES_USER', 'darkstore')}:{e['POSTGRES_PASSWORD']}@127.0.0.1:5433/{e.get('POSTGRES_DB', 'darkstore')}"
        return cls(await asyncpg.create_pool(dsn, min_size=1, max_size=3))

    async def stock_total(self) -> int:
        a = await self.p.fetchval("select coalesce(sum(available),0) from inventory.stock")
        r = await self.p.fetchval("""select coalesce(sum((i->>'qty')::int),0) from inventory.reservations r,
            json_array_elements(r.items) i where r.status='reserved'""")
        return int(a) + int(r)

    async def active_orders(self) -> int:
        return await self.p.fetchval(
            "select count(*) from orders.orders where status not in ('delivered','cancelled','rejected')")

    async def statuses(self, ids: list[str]) -> dict[str, str]:
        rows = await self.p.fetch("select order_id, status from orders.orders where order_id = any($1)", ids)
        return {r["order_id"]: r["status"] for r in rows}

    async def evidence_rows(self, ids: list[str], customer: str):
        ev = {}
        ev["rows"] = await self.p.fetchval("select count(*) from orders.orders where customer_id=$1", customer)
        out = await self.p.fetch("""select body::json->>'order_id' o, body::json->>'event_type' t
            from orders.outbox where body::json->>'order_id' = any($1)
              and body::json->>'event_type' in ('order.created','order.delivered')""", ids)
        ev["created"] = Counter(r["o"] for r in out if r["t"] == "order.created")
        ev["delivered"] = Counter(r["o"] for r in out if r["t"] == "order.delivered")
        ev["log"] = [(r["order_id"], r["rider_id"], r["action"]) for r in await self.p.fetch(
            "select order_id, rider_id, action from dispatch.assignment_log where order_id = any($1) order by id", ids)]
        ev["res"] = {r["order_id"]: r["status"] for r in await self.p.fetch(
            "select order_id, status from inventory.reservations where order_id = any($1)", ids)}
        return ev


def rpk(*args: str) -> str:
    """rpk can time out while the broker is busy or just restarted: retry before believing it."""
    for attempt in range(6):
        out = compose("exec", "-T", "redpanda", "rpk", *args, timeout=60)
        if "i/o timeout" not in out and "unable to request metadata" not in out:
            return out
        time.sleep(5)
    return out


def dlq_total() -> int:
    """Messages ever written to any dead-letter topic (high watermarks; survives consumer restarts).

    Raises if it cannot read them: a baseline of 0 from a parse failure would hide real dead letters."""
    out = rpk("topic", "list")
    topics = [ln.split()[0] for ln in out.splitlines() if ln.split() and ".dlq" in ln.split()[0]]
    if not topics:
        raise RuntimeError(f"no dead-letter topics found in: {out[:200]!r}")
    total = 0
    for t in topics:
        d = rpk("topic", "describe", t, "-p")
        rows = [ln.split() for ln in d.splitlines()]
        hdr = next((r for r in rows if "HIGH-WATERMARK" in r), None)
        if hdr is None:
            raise RuntimeError(f"cannot parse partitions of {t}: {d[:200]!r}")
        i = hdr.index("HIGH-WATERMARK")
        total += sum(int(r[i]) for r in rows[rows.index(hdr) + 1:] if len(r) > i and r[i].isdigit())
    return total


# ---------------------------------------------------------------------------------------------- run

async def inject(kind: str, target: str) -> dict:
    t0 = time.time()
    log(f"FAULT {kind} {target}")
    if kind == "kill":
        compose("kill", target)
        await asyncio.sleep(OUTAGE_S)
        compose("start", target)
    else:
        compose("restart", target)
    log(f"FAULT over after {time.time() - t0:.0f}s: {target} is back")
    return {"kind": kind, "target": target, "seconds": round(time.time() - t0, 1)}


async def main(scenario: str, orders: int, rate: float, fault_at: float, settle: float) -> int:
    kind, target = SCENARIOS[scenario]
    run_id = uuid.uuid4().hex[:8]
    db = await Db.connect()
    log(f"run {run_id}: {scenario}, {orders} orders at {rate}/s, fault at +{fault_at}s")

    # preflight: a quiet, healthy system, else the result says nothing about the fault
    for _ in range(450):   # up to 15 min: leftovers from an earlier run time out and cancel on their own
        if await db.active_orders() < 5:
            break
        await asyncio.sleep(2)
    else:
        log("preflight failed: the system is not quiet (active orders)")
        return 2
    stock_before, dlq_before = await db.stock_total(), dlq_total()
    log(f"baseline: stock+reserved={stock_before} dlq_messages={dlq_before}")

    rng = random.Random(run_id)
    client = Client(run_id, rng)
    deadline = time.time() + fault_at + settle
    watchers: dict[str, WsResult] = {}
    place_tasks: list[asyncio.Task] = []
    watch_tasks: list[asyncio.Task] = []
    fault_task: asyncio.Task | None = None
    t_start = time.time()

    def start_watchers() -> None:   # gateway scenario: follow the first accepted orders live, before the fault
        for oid, tok in list(client.tokens.items()):
            if scenario == "kill-gateway" and oid not in watchers and len(watchers) < 12:
                watchers[oid] = WsResult()
                watch_tasks.append(asyncio.create_task(watch(oid, tok, watchers[oid], deadline + 120)))

    async with httpx.AsyncClient(base_url=ORDER_API, timeout=10) as http:
        await client.open_session(http)
        customer = client.session["customer_id"]   # server-assigned; the run's orders are found by it
        for i in range(orders):
            place_tasks.append(asyncio.create_task(client.place(http, i)))
            start_watchers()
            if fault_task is None and time.time() - t_start >= fault_at:
                fault_task = asyncio.create_task(inject(kind, target))
            await asyncio.sleep(1 / rate)
        if fault_task is None:
            fault_task = asyncio.create_task(inject(kind, target))
        await asyncio.gather(*place_tasks)
        start_watchers()
    fault = await fault_task
    log(f"all requests settled: {sum(len(v) > 0 for v in client.attempts.values())}/{orders} accepted, "
        f"{client.failed_attempts} failed attempts, {len(client.gave_up)} gave up")

    ids = sorted({o for s in client.attempts.values() for o in s})
    final: dict[str, str] = {}
    while time.time() < deadline + 120:
        final = await db.statuses(ids)
        left = [o for o in ids if final.get(o) not in TERMINAL]
        if not left:
            break
        await asyncio.sleep(3)
    for w in watch_tasks:
        await asyncio.wait({w}, timeout=30)
    rows = await db.evidence_rows(ids, customer)
    ev = Evidence(attempts=client.attempts, orders=final, order_rows_for_run=rows["rows"],
                  delivered_events=rows["delivered"], created_events=rows["created"],
                  assignment_log=rows["log"], reservations=rows["res"], stock_before=stock_before,
                  stock_after=await db.stock_total(), dlq_before=dlq_before, dlq_after=dlq_total(), ws=watchers)
    found = violations(ev) + [f"GAVE UP: {k} never got a response in 150 s" for k in client.gave_up]
    counts = Counter(final.values())
    lat = sorted(client.latencies)
    pct = lambda p: round(lat[min(len(lat) - 1, int(len(lat) * p))] * 1000, 1) if lat else None  # noqa: E731
    result = {"scenario": scenario, "run_id": run_id, "orders": orders, "rate_per_s": rate, "fault": fault,
              "accepted": len(ids), "final_statuses": dict(counts), "failed_attempts": client.failed_attempts,
              "create_latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "max": round(lat[-1] * 1000, 1) if lat else None},
              "ws_clients": len(watchers), "ws_reconnects": sum(w.reconnects for w in watchers.values()),
              "stock_before": stock_before, "stock_after": ev.stock_after,
              "dlq_before": dlq_before, "dlq_after": ev.dlq_after,
              "assignment_log_rows": len(rows["log"]), "violations": found, "passed": not found}
    out = ROOT / "docs" / "chaos"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{scenario}.json").write_text(json.dumps(result, indent=2) + "\n")
    log(json.dumps({k: v for k, v in result.items() if k != "violations"}))
    for x in found[:20]:
        log(f"VIOLATION {x}")
    log("PASS" if not found else f"FAIL ({len(found)} violations)")
    return 0 if not found else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--orders", type=int, default=120)
    ap.add_argument("--rate", type=float, default=2.0)
    ap.add_argument("--fault-at", type=float, default=20.0)
    ap.add_argument("--settle", type=float, default=600.0, help="seconds to wait for delivery after the last order")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.scenario, a.orders, a.rate, a.fault_at, a.settle)))
