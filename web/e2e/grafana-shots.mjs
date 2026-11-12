// Screenshots of the five Grafana dashboards and of one order's trace (docs/screenshots/).
// Usage: node e2e/grafana-shots.mjs <order_id>      (reads GRAFANA_ADMIN_PASSWORD from ../.env)
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { chromium } from '@playwright/test';

const orderId = process.argv[2];
if (!orderId) throw new Error('usage: node e2e/grafana-shots.mjs <order_id>');
const env = Object.fromEntries(readFileSync('../.env', 'utf8').split('\n')
  .filter((l) => l.includes('=') && !l.startsWith('#')).map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]));
const base = process.env.GRAFANA_URL ?? 'http://localhost:3000';
if (!/^(https:|http:\/\/(localhost|127\.0\.0\.1)(:|\/|$))/.test(base)) {
  throw new Error('refusing to send the Grafana admin password over plain http to a non-local host');
}
const traceId = createHash('sha256').update(`trace:${orderId}`).digest().subarray(0, 16).toString('hex');
const out = '../docs/screenshots';

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
const login = await ctx.request.post(`${base}/login`, { data: { user: 'admin', password: env.GRAFANA_ADMIN_PASSWORD } });
if (!login.ok()) throw new Error(`grafana login failed: ${login.status()}`);
const page = await ctx.newPage();

const panes = { a: { datasource: 'tempo', queries: [{ refId: 'A', datasource: { type: 'tempo', uid: 'tempo' }, queryType: 'traceql', query: traceId }], range: { from: 'now-6h', to: 'now' } } };
await page.setViewportSize({ width: 1600, height: 2000 });   // tall: the span tree scrolls inside the page
await page.goto(`${base}/explore?schemaVersion=1&orgId=1&panes=${encodeURIComponent(JSON.stringify(panes))}`);
await page.getByText('order.create').first().waitFor({ timeout: 30000 });
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/trace-one-order.png`, fullPage: true });
console.log(`trace ${traceId} -> trace-one-order.png`);

await page.setViewportSize({ width: 1600, height: 1100 });
for (const [uid, file] of [['darkstore-order-funnel', 'grafana-1-order-funnel'], ['darkstore-kafka', 'grafana-2-kafka'],
  ['darkstore-matching', 'grafana-3-matching'], ['darkstore-eta', 'grafana-4-eta'], ['darkstore-websocket', 'grafana-5-websocket']]) {
  await page.goto(`${base}/d/${uid}?orgId=1&from=now-30m&to=now&kiosk`);
  await page.waitForTimeout(7000);   // let every panel query finish
  await page.screenshot({ path: `${out}/${file}.png`, fullPage: true });
  console.log(`${uid} -> ${file}.png`);
}
await browser.close();
