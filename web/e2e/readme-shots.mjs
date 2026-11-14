// Screenshots for the README (docs/screenshots/): one order followed frame by frame, and the live ops dashboard.
// Usage (stack + simulator up, run from web/):  OPS_TOKEN=<token> node e2e/readme-shots.mjs
// The ops token comes from `make urls` (the part after #token=). Without it only the customer frames are taken.
import { chromium, expect } from '@playwright/test';

const base = process.env.BASE_URL ?? 'http://localhost:8088';
if (!/^http:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(base)) throw new Error('refusing to send an ops token to a non-local host');
const out = '../docs/screenshots';
const shot = async (page, name) => {
  await page.screenshot({ path: `${out}/${name}.jpg`, type: 'jpeg', quality: 82 });
  console.log(`-> ${name}.jpg`);
};

const want = new Set((process.env.SHOTS ?? 'customer,ops').split(','));   // SHOTS=customer or SHOTS=ops to take one part
const browser = await chromium.launch();

// customer: place an order, then capture picking -> out for delivery -> delivered
if (want.has('customer')) {
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
await page.goto(base);
await page.getByTestId('place-order').click();
await expect(page.getByTestId('connection-state')).toHaveText(/live/i, { timeout: 30_000 });
await expect(page.getByTestId('marker-store')).toBeVisible({ timeout: 10_000 });   // the map has fitted to the order
await expect(page.getByTestId('step-picking')).toHaveAttribute('data-state', 'active', { timeout: 60_000 });
await page.waitForTimeout(1500);   // let the map tiles load
await shot(page, 'customer-1-picking');
await expect(page.getByTestId('step-out_for_delivery')).toHaveAttribute('data-state', 'active', { timeout: 240_000 });
await expect(page.getByTestId('marker-rider')).toBeVisible();
await expect(page.getByTestId('eta-range')).toContainText(/\d|<1/, { timeout: 20_000 });   // not "Estimating..."
await page.waitForTimeout(1500);
await shot(page, 'customer-2-out-for-delivery');
await expect(page.getByTestId('order-status')).toHaveText(/delivered/i, { timeout: 240_000 });
await page.waitForTimeout(1000);
await shot(page, 'customer-3-delivered');
}

// ops dashboard with the demo running
const token = process.env.OPS_TOKEN;
if (want.has('ops') && token) {
  const ops = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await ops.goto(`${base}/ops/#token=${token}`);
  await expect(ops.getByTestId('ops-riders-count')).toHaveAttribute('data-total', /^[1-9]\d*$/, { timeout: 30_000 });
  await ops.waitForTimeout(5000);   // a few polls, so the funnel and late-orders table have data
  await shot(ops, 'ops-live');
}
await browser.close();
