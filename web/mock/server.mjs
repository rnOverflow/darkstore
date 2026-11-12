// Mock DarkStore backend implementing docs/api/realtime.md (HTTP + WebSocket).
//   PORT (8088)  OPS_TOKEN (ops-demo-token)  DELIVERY_S (24)  HEARTBEAT_MS (15000)
// Extras for testing:  POST /mock/kick?code=1006   closes every open /ws socket with that code.
import http from 'node:http';
import { randomUUID, randomBytes } from 'node:crypto';
import { WebSocketServer } from 'ws';

const PORT = Number(process.env.PORT ?? 8088);
const OPS_TOKEN = process.env.OPS_TOKEN ?? 'ops-demo-token';
const DELIVERY_S = Number(process.env.DELIVERY_S ?? 24);
const HEARTBEAT_MS = Number(process.env.HEARTBEAT_MS ?? 15000);

const CITY = {
  center: [12.9716, 77.5946],
  bounds: [[12.9116, 77.5346], [13.0316, 77.6546]],
  stores: {
    'store-0': [12.9352, 77.6245],
    'store-1': [12.9784, 77.6408],
    'store-2': [12.9141, 77.5773],
    'store-3': [13.0012, 77.5513],
  },
  sim_speed: 30,
};

// ---- stream ids -------------------------------------------------------------------------------
let lastMs = 0;
let lastSeq = 0;
function nextId() {
  const ms = Date.now();
  if (ms > lastMs) { lastMs = ms; lastSeq = 0; } else { lastSeq += 1; }
  return `${lastMs}-${lastSeq}`;
}

// ---- orders -----------------------------------------------------------------------------------
const orders = new Map(); // id -> order
const sockets = new Set();
const STAGE = { created: 'created', reserved: 'created', picking: 'picking', picked: 'picking', assigned: 'assigned', out_for_delivery: 'out_for_delivery' };
const TOTAL_S = 10 + DELIVERY_S;

function emit(o, kind, data) {
  const ev = { type: 'event', id: nextId(), kind, data };
  o.log.push(ev);
  if (o.log.length > 2000) o.log.shift();
  for (const s of o.subs) send(s, ev);
  return ev;
}

function setStatus(o, status) {
  o.status = status;
  o.history.push({ status, at: new Date().toISOString() });
  emit(o, 'status', { status, at: new Date().toISOString() });
}

function emitEta(o) {
  const elapsed = (Date.now() - o.createdAt) / 1000;
  const p50 = Math.max(4, TOTAL_S - elapsed);
  o.eta = {
    stage: STAGE[o.status] ?? 'out_for_delivery',
    p50_real_s: Math.round(p50 * 10) / 10,
    p90_real_s: Math.round((p50 * 1.4 + 4) * 10) / 10,
    source: elapsed % 4 < 2 ? 'model' : 'heuristic',
    at: new Date().toISOString(),
  };
  emit(o, 'eta', o.eta);
}

function tick(o) {
  const t = (Date.now() - o.createdAt) / 1000;
  const steps = [['reserved', 1], ['picking', 2.5], ['picked', 7], ['assigned', 8], ['out_for_delivery', 10]];
  for (const [st, at] of steps) {
    if (t >= at && o.reached < steps.findIndex(([s]) => s === st) + 1) {
      o.reached = steps.findIndex(([s]) => s === st) + 1;
      setStatus(o, st);
      if (st === 'assigned') o.rider = { rider_id: o.riderId, lat: o.store[0], lng: o.store[1] };
    }
  }
  if (o.status === 'out_for_delivery' || o.status === 'assigned') {
    const f = Math.min(1, Math.max(0, (t - 10) / DELIVERY_S));
    o.rider = { rider_id: o.riderId, lat: o.store[0] + (o.dest[0] - o.store[0]) * f, lng: o.store[1] + (o.dest[1] - o.store[1]) * f };
    if (o.status === 'out_for_delivery') emit(o, 'position', { ...o.rider, at: new Date().toISOString() });
  }
  if (Math.floor(t) % 2 === 0 && o.status !== 'delivered') emitEta(o);
  if (t >= TOTAL_S) {
    setStatus(o, 'delivered');
    o.eta = null;
    clearInterval(o.timer);
  }
}

function createOrder(body) {
  const id = randomUUID();
  const store = CITY.stores[body.store_id];
  const o = {
    id, token: randomBytes(16).toString('hex'), status: 'created', storeId: body.store_id, store, dest: [body.lat, body.lng],
    createdAt: Date.now(), reached: 0, history: [], log: [], subs: new Set(), eta: null, rider: null,
    riderId: `rider-${Math.floor(Math.random() * 6)}`, items: body.items,
  };
  orders.set(id, o);
  o.history.push({ status: 'created', at: new Date().toISOString() });
  emit(o, 'status', { status: 'created', at: new Date().toISOString() });
  emitEta(o);
  o.timer = setInterval(() => tick(o), 1000);
  return o;
}

function validOrder(b) {
  if (!b || typeof b !== 'object') return 'body must be an object';
  if (typeof b.customer_id !== 'string') return 'customer_id';
  if (!CITY.stores[b.store_id]) return 'store_id';
  if (!Number.isFinite(b.lat) || !Number.isFinite(b.lng)) return 'lat/lng';
  if (!Array.isArray(b.items) || b.items.length < 1 || b.items.length > 4) return 'items (1-4)';
  for (const it of b.items) {
    if (!/^sku-(\d|1\d)$/.test(it?.sku ?? '')) return 'sku';
    if (!Number.isInteger(it.qty) || it.qty < 1 || it.qty > 3) return 'qty (1-3)';
  }
  return null;
}

// ---- ops data ---------------------------------------------------------------------------------
const ALL_STATUSES = ['created', 'reserved', 'picking', 'picked', 'assigned', 'out_for_delivery', 'delivered', 'cancelled', 'rejected'];
const SEED_RIDERS = Array.from({ length: 12 }, (_, i) => {
  const [la, ln] = Object.values(CITY.stores)[i % 4];
  return { id: `rider-${i}`, base: [la + (Math.sin(i * 7) * 0.012), ln + (Math.cos(i * 5) * 0.012)], phase: i };
});

function opsSummary() {
  const funnel = Object.fromEntries(ALL_STATUSES.map((s) => [s, 0]));
  let last5 = 0;
  for (const o of orders.values()) {
    funnel[o.status] += 1;
    if (Date.now() - o.createdAt < 300_000) last5 += 1;
  }
  // baseline so the funnel has shape even with no demo orders
  const base = { created: 3, reserved: 2, picking: 4, picked: 2, assigned: 2, out_for_delivery: 5, delivered: 40, cancelled: 2, rejected: 1 };
  for (const k of ALL_STATUSES) funnel[k] += base[k];
  const r = opsRiders();
  const riders = { idle: 0, assigned: 0, cooldown: 0 };
  r.forEach((x) => { riders[x.status] += 1; });
  return {
    funnel, pick_p95_sim_min: 4.2 + (Math.sin(Date.now() / 20000) + 1), pick_samples: 120 + orders.size,
    orders_last_5m: 18 + last5, riders, sim_speed: CITY.sim_speed, generated_at: new Date().toISOString(),
  };
}

function opsRiders() {
  const t = Date.now() / 1000;
  const busy = new Map();
  for (const o of orders.values()) if (o.rider && o.status !== 'delivered') busy.set(o.riderId, o);
  return SEED_RIDERS.map((s) => {
    const o = busy.get(s.id);
    if (o) return { id: s.id, lat: o.rider.lat, lng: o.rider.lng, status: 'assigned', order_id: o.id };
    const cooldown = Math.floor((t / 15 + s.phase) % 4) === 0;
    return {
      id: s.id, lat: s.base[0] + Math.sin(t / 30 + s.phase) * 0.002, lng: s.base[1] + Math.cos(t / 30 + s.phase) * 0.002,
      status: cooldown ? 'cooldown' : s.phase % 3 === 0 ? 'assigned' : 'idle', order_id: null,
    };
  });
}

function hexBoundary(lat, lng, r) {
  return Array.from({ length: 6 }, (_, i) => {
    const a = (Math.PI / 3) * i + Math.PI / 6;
    return [lat + r * Math.sin(a), lng + (r * Math.cos(a)) / Math.cos((lat * Math.PI) / 180)];
  });
}

function opsHeatmap() {
  const t = Date.now() / 1000;
  const cells = [];
  const r = 0.0042;
  let n = 0;
  for (let i = -4; i <= 4; i++) {
    for (let j = -4; j <= 4; j++) {
      const lat = CITY.center[0] + i * r * 1.75;
      const lng = CITY.center[1] + (j + (i % 2 ? 0.5 : 0)) * r * 2 / Math.cos((CITY.center[0] * Math.PI) / 180);
      const demand = Math.max(0, Math.round(3 + 3 * Math.sin(i * 1.3 + t / 40) + 2 * Math.cos(j * 0.9 - t / 25)));
      const supply = Math.max(0, Math.round(2 + 2 * Math.cos(i * 0.7 - t / 33) + 1.5 * Math.sin(j * 1.1 + t / 50)));
      cells.push({ cell: `89mock${String(n++).padStart(4, '0')}`, supply, demand, boundary: hexBoundary(lat, lng, r) });
    }
  }
  return { cells };
}

function opsLate() {
  const late = [];
  const mk = (id, status, store, elapsed, promised) => late.push({
    order_id: id, status, store_id: store, elapsed_sim_min: elapsed, promised_p90_sim_min: promised, over_by_sim_min: Math.round((elapsed - promised) * 10) / 10,
  });
  mk('a1b2c3d4-0000-0000-0000-000000000001', 'picking', 'store-1', 21.4, 12.0);
  mk('e5f6a7b8-0000-0000-0000-000000000002', 'assigned', 'store-0', 17.2, 13.5);
  for (const o of orders.values()) {
    const elapsedSim = ((Date.now() - o.createdAt) / 1000) * CITY.sim_speed / 60;
    if (o.status !== 'delivered' && elapsedSim > 8) mk(o.id, o.status, o.storeId, Math.round(elapsedSim * 10) / 10, 8);
  }
  late.sort((a, b) => b.over_by_sim_min - a.over_by_sim_min);
  return { late: late.slice(0, 20) };
}

// ---- HTTP -------------------------------------------------------------------------------------
function json(res, code, body) {
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}
function readBody(req) {
  return new Promise((resolve) => {
    let d = '';
    req.on('data', (c) => { d += c; });
    req.on('end', () => { try { resolve(JSON.parse(d)); } catch { resolve(null); } });
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const path = url.pathname;
  if (req.method === 'GET' && path === '/api/city') return json(res, 200, CITY);
  if (req.method === 'POST' && path === '/api/orders') {
    const body = await readBody(req);
    const bad = validOrder(body);
    if (bad) return json(res, 422, { detail: `invalid: ${bad}` });
    const o = createOrder(body);
    return json(res, 201, { order_id: o.id, status: 'created', tracking_token: o.token });
  }
  const m = /^\/api\/orders\/([^/]+)$/.exec(path);
  if (req.method === 'GET' && m) {
    const o = orders.get(m[1]);
    return o ? json(res, 200, { order_id: o.id, status: o.status, store_id: o.storeId, items: o.items, lat: o.dest[0], lng: o.dest[1] }) : json(res, 404, { detail: 'not found' });
  }
  if (path.startsWith('/api/ops/')) {
    if (req.headers.authorization !== `Bearer ${OPS_TOKEN}`) return json(res, 401, { detail: 'unauthorized' });
    if (path === '/api/ops/summary') return json(res, 200, opsSummary());
    if (path === '/api/ops/riders') return json(res, 200, { riders: opsRiders() });
    if (path === '/api/ops/heatmap') return json(res, 200, opsHeatmap());
    if (path === '/api/ops/late') return json(res, 200, opsLate());
  }
  if (req.method === 'POST' && path === '/mock/kick') {
    const code = Number(url.searchParams.get('code') ?? 1006);
    for (const s of sockets) s.close(code === 1006 ? 4000 : code);
    return json(res, 200, { kicked: sockets.size });
  }
  return json(res, 404, { detail: 'not found' });
});

// ---- WebSocket --------------------------------------------------------------------------------
const wss = new WebSocketServer({ server, path: '/ws' });

function send(ws, obj) {
  if (ws.readyState === 1) ws.send(JSON.stringify(obj));
}
function fail(ws, code, wsCode, message) {
  send(ws, { type: 'error', code, message });
  ws.close(wsCode, code);
}

wss.on('connection', (ws) => {
  sockets.add(ws);
  let role = null;
  let token = null;
  let sub = null;
  const authTimer = setTimeout(() => { if (!role) fail(ws, 'timeout', 4408, 'auth timeout'); }, 5000);
  const hb = setInterval(() => send(ws, { type: 'heartbeat', ts: Math.floor(Date.now() / 1000) }), HEARTBEAT_MS);
  ws.on('close', () => {
    clearTimeout(authTimer); clearInterval(hb); sockets.delete(ws);
    if (sub) sub.subs.delete(ws);
  });
  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch { return fail(ws, 'bad_request', 4400, 'bad json'); }
    if (!role) {
      if (msg.type !== 'auth') return fail(ws, 'unauthorized', 4401, 'auth first');
      if (msg.token === OPS_TOKEN) role = 'ops';
      else if ([...orders.values()].some((o) => o.token === msg.token)) role = 'customer';
      else return fail(ws, 'unauthorized', 4401, 'bad token');
      token = msg.token;
      clearTimeout(authTimer);
      return send(ws, { type: 'auth_ok', role });
    }
    if (msg.type === 'pong') return;
    if (msg.type === 'subscribe') {
      const o = orders.get(msg.order_id);
      if (role === 'customer' && (!o || o.token !== token)) return fail(ws, 'forbidden', 4403, 'not your order');
      if (!o) return fail(ws, 'bad_request', 4400, 'unknown order');
      if (sub) sub.subs.delete(ws);
      // snapshot first (reflects everything up to the last logged event), then live events.
      const last = o.log.length ? o.log[o.log.length - 1].id : null;
      send(ws, {
        type: 'snapshot', order_id: o.id, last_event_id: last, status: o.status,
        store: { id: o.storeId, lat: o.store[0], lng: o.store[1] }, dest: { lat: o.dest[0], lng: o.dest[1] },
        eta: o.eta, rider: o.rider, history: o.history,
      });
      sub = o;
      o.subs.add(ws);
      return undefined;
    }
    return fail(ws, 'bad_request', 4400, 'unknown message');
  });
});

server.listen(PORT, '127.0.0.1', () => console.log(`mock backend on http://127.0.0.1:${PORT} (ops token: ${OPS_TOKEN})`));
