import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Backend (nginx in compose, the mock server by default) that /api and /ws are proxied to in dev.
const target = process.env.DARKSTORE_BACKEND ?? 'http://localhost:8088';

export default defineConfig({
  base: '/',
  appType: 'mpa',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target, changeOrigin: true },
      '/ws': { target, ws: true, changeOrigin: true },
    },
  },
  build: {
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      input: {
        customer: resolve(__dirname, 'index.html'),
        ops: resolve(__dirname, 'ops/index.html'),
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
