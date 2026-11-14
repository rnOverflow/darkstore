import { expect, test } from '@playwright/test';

const SHOTS = '../docs/screenshots';

test('customer places an order and watches it get delivered', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('place-order').click();

  // the order is created and the live connection comes up
  const orderId = page.getByTestId('order-id');
  await expect(orderId).toBeVisible();
  await expect(page.getByTestId('connection-state')).toHaveText(/live/i, { timeout: 30_000 });
  await expect(page.getByTestId('stepper')).toBeVisible();
  await expect(page.getByTestId('customer-map')).toBeVisible();

  // it moves forward through the pipeline and shows an ETA range while it travels
  await expect(page.getByTestId('order-status')).not.toHaveText(/^$/);
  await expect(page.getByTestId('step-created')).toHaveAttribute('data-state', /done|active/);
  await expect(page.getByTestId('step-out_for_delivery')).toHaveAttribute('data-state', 'active', {
    timeout: 240_000,
  });
  await expect(page.getByTestId('marker-rider')).toBeVisible();
  // store and customer markers: from the first snapshot, or from the meta event when the page connected before the
  // gateway had consumed order.created (this used to leave the map zoomed out on the whole city, with neither marker)
  await expect(page.getByTestId('marker-store')).toBeVisible();
  await expect(page.getByTestId('marker-customer')).toBeVisible();
  await expect(page.getByTestId('eta-range')).toContainText(/\d|<1/);
  await page.screenshot({ path: `${SHOTS}/customer-out-for-delivery.png` });

  // and ends delivered, with every earlier step marked done
  await expect(page.getByTestId('order-status')).toHaveText(/delivered/i, { timeout: 240_000 });
  for (const s of ['created', 'reserved', 'picking', 'picked', 'assigned', 'out_for_delivery']) {
    await expect(page.getByTestId(`step-${s}`)).toHaveAttribute('data-state', 'done');
  }
  await expect(page.getByTestId('step-delivered')).toHaveAttribute('data-state', /done|active/);
  await page.screenshot({ path: `${SHOTS}/customer-delivered.png`, fullPage: true });
});

test('tracking survives a dropped network connection and resumes', async ({ page, context }) => {
  await page.goto('/');
  await page.getByTestId('place-order').click();
  await expect(page.getByTestId('connection-state')).toHaveText(/live/i, { timeout: 30_000 });

  await context.setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event('offline')));
  // the browser keeps the socket until it notices; the client must detect/recover either way
  await page.waitForTimeout(3_000);
  await context.setOffline(false);
  await expect(page.getByTestId('connection-state')).toHaveText(/live/i, { timeout: 60_000 });
  await expect(page.getByTestId('order-status')).not.toHaveText(/^$/);
});

test('a reload resumes tracking the same order', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('place-order').click();
  await expect(page.getByTestId('connection-state')).toHaveText(/live/i, { timeout: 30_000 });
  const id = (await page.getByTestId('order-id').textContent())?.trim();
  expect(id).toBeTruthy();
  await page.reload();
  await expect(page.getByTestId('connection-state')).toHaveText(/live/i, { timeout: 30_000 });
  await expect(page.getByTestId('order-id')).toContainText(id!);
});

test('a bad tracking token shows an expired session and does not retry forever', async ({ page }) => {
  await page.goto('/#order=00000000-0000-0000-0000-000000000000&token=not-a-real-token');
  await expect(page.getByTestId('session-expired')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('connection-state')).toHaveText(/expired/i);
});

test('ops dashboard shows the live city', async ({ page }) => {
  const token = process.env.OPS_TOKEN;
  test.skip(!token, 'set OPS_TOKEN (make e2e does this)');
  // MapLibre parses GeoJSON (riders, heat cells) in a web worker; if the worker file is not served as JavaScript the
  // layers silently stay empty and only this console error shows it.
  const workerErrors: string[] = [];
  page.on('console', (m) => {
    if (m.type() === 'error' && /worker/i.test(m.text())) workerErrors.push(m.text());
  });
  await page.goto(`/ops/#token=${token}`);
  await expect(page.getByTestId('ops-map')).toBeVisible();
  await expect(page.getByTestId('ops-funnel')).toBeVisible();
  await expect(page.getByTestId('ops-pick-p95')).toBeVisible();
  await expect(page.getByTestId('ops-late-table')).toBeVisible();
  // the fleet is visible: rider counts populate from /api/ops/summary
  await expect(page.getByTestId('ops-riders-count')).toHaveAttribute('data-total', /^[1-9]\d*$/, {
    timeout: 30_000,
  });
  await page.waitForTimeout(3_000); // let a couple of polls and the map settle
  expect(workerErrors).toEqual([]);
  await page.screenshot({ path: `${SHOTS}/ops-dashboard.png`, fullPage: true });
});

test('ops dashboard rejects a bad token without polling forever', async ({ page }) => {
  await page.goto('/ops/#token=garbage');
  await expect(page.getByTestId('ops-expired')).toBeVisible({ timeout: 20_000 });
});
