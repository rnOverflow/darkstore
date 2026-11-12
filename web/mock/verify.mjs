// Contract check against a running mock (or real backend): node mock/verify.mjs [base] [opsToken]
import WebSocket from 'ws';
const base = process.argv[2] ?? 'http://127.0.0.1:8088';
const opsToken = process.argv[3] ?? 'ops-demo-token';
const wsUrl = base.replace(/^http/, 'ws') + '/ws';
let fails = 0;
const ok = (c, m) => { console.log(`${c ? 'PASS' : 'FAIL'} ${m}`); if (!c) fails++; };
const cmp = (a, b) => { const [am, as] = a.split('-').map(Number); const [bm, bs] = b.split('-').map(Number); return am - bm || as - bs; };

const city = await (await fetch(`${base}/api/city`)).json();
ok(Array.isArray(city.center) && city.stores && Array.isArray(city.bounds), '/api/city shape');
const [sid, [slat, slng]] = Object.entries(city.stores)[0];
const r = await fetch(`${base}/api/orders`, { method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ customer_id: 'demo-customer', store_id: sid, items: [{ sku: 'sku-3', qty: 2 }], lat: slat + 0.01, lng: slng + 0.01 }) });
const order = await r.json();
ok(r.status === 201 && order.order_id && order.tracking_token && order.status === 'created', 'POST /api/orders -> 201');
for (const p of ['summary', 'riders', 'heatmap', 'late']) {
  ok((await fetch(`${base}/api/ops/${p}`)).status === 401, `/api/ops/${p} without token -> 401`);
  const rr = await fetch(`${base}/api/ops/${p}`, { headers: { authorization: `Bearer ${opsToken}` } });
  const b = await rr.json();
  ok(rr.status === 200 && (p === 'summary' ? b.funnel && 'pick_p95_sim_min' in b && b.riders : b[{ riders: 'riders', heatmap: 'cells', late: 'late' }[p]]), `/api/ops/${p} shape`);
}
const connect = (token, orderId, last = null) => new Promise((resolve) => {
  const ws = new WebSocket(wsUrl); const frames = [];
  ws.on('open', () => ws.send(JSON.stringify({ type: 'auth', token })));
  ws.on('message', (d) => { const m = JSON.parse(d); frames.push(m); if (m.type === 'auth_ok') ws.send(JSON.stringify({ type: 'subscribe', order_id: orderId, last_event_id: last })); });
  ws.on('close', (code) => resolve({ ws, frames, code }));
  resolve.ws = ws;
  setTimeout(() => ws.close(), 9000);
  connect.last = { ws, frames };
});
// bad token
const bad = await connect('nope', order.order_id);
ok(bad.code === 4401, `bad token closes 4401 (got ${bad.code})`);
const otherOrder = await (await fetch(`${base}/api/orders`, { method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ customer_id: 'x', store_id: sid, items: [{ sku: 'sku-1', qty: 1 }], lat: slat, lng: slng }) })).json();
const forb = await connect(otherOrder.tracking_token, order.order_id);
ok(forb.code === 4403, `other order's token closes 4403 (got ${forb.code})`);
// happy path: ~9 s window
const happy = await connect(order.tracking_token, order.order_id);
const f = happy.frames;
ok(f[0]?.type === 'auth_ok' && f[0].role === 'customer', 'auth_ok first');
ok(f[1]?.type === 'snapshot' && f[1].order_id === order.order_id && f[1].dest && f[1].store, 'snapshot immediately after subscribe');
const evs = f.filter((x) => x.type === 'event');
ok(evs.length > 3, `received ${evs.length} events`);
ok(evs.every((e, i) => i === 0 || cmp(evs[i - 1].id, e.id) < 0), 'event ids strictly increasing as (ms,seq)');
ok(evs.some((e) => e.kind === 'status') && evs.some((e) => e.kind === 'eta'), 'status + eta events seen');
const sts = evs.filter((e) => e.kind === 'status').map((e) => e.data.status);
console.log('statuses seen:', sts.join(' > '));
// resume
const snapId = f[1].last_event_id;
const resumed = await connect(order.tracking_token, order.order_id, evs.at(-1)?.id ?? snapId);
ok(resumed.frames[1]?.type === 'snapshot', 'resume: snapshot first again');
process.exit(fails ? 1 : 0);
