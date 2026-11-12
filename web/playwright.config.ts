import { defineConfig } from '@playwright/test';

// Runs against the real stack (`make up`): nginx on :8088 serves the UIs and proxies /ws and /api.
export default defineConfig({
  testDir: './e2e',
  outputDir: './e2e/results',
  timeout: 360_000, // a real delivery takes about a minute of wall-clock time at 30x sim speed
  expect: { timeout: 20_000 },
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:8088',
    viewport: { width: 1280, height: 800 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
