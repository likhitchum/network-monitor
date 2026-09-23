// @ts-check
import { defineConfig, devices } from '@playwright/test';

/**
 * E2E tests run against a real docker compose stack (nginx + FastAPI + SQLite).
 * Start it before running:  docker compose up -d --build --wait
 * Env: E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD must match the seeded admin account.
 */
export default defineConfig({
  testDir: './tests',
  timeout: 40_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8080',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
