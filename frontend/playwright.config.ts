import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

// Browser tests for the real dashboard: the PRODUCTION BUILD of the frontend served by `vite preview`, talking through its
// proxy to the REAL API, which runs against a throwaway database (frontend/e2e/support/run_api.py). Nothing about sign-in,
// sessions, cookies or roles is mocked.
const API_PORT = Number(process.env.E2E_API_PORT || 8765);
const WEB_PORT = Number(process.env.E2E_WEB_PORT || 4173);
const repoRoot = path.resolve(import.meta.dirname, "..");

export default defineConfig({
  testDir: "./e2e/tests",
  outputDir: "./test-results",
  globalTeardown: "./e2e/support/global-teardown.ts",
  fullyParallel: true,
  workers: process.env.CI ? 2 : 3,
  retries: process.env.CI ? 1 : 0,
  forbidOnly: !!process.env.CI,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // The production dashboard is a single-origin app; a browser locale/timezone would only add noise to assertions.
    locale: "en-US",
    timezoneId: "UTC",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      // Local mail capture: the API's reset emails arrive here (SMTP) and the tests read them (JSON). Nothing leaves the machine.
      command: "uv run python frontend/e2e/support/mail_server.py",
      cwd: repoRoot,
      url: `http://127.0.0.1:${process.env.E2E_MAIL_API_PORT || 8026}/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
      stdout: "ignore",
    },
    {
      // Creates the database, migrates it, runs the API; deletes the database on exit.
      command: "uv run python frontend/e2e/support/run_api.py",
      cwd: repoRoot,
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 30_000 },
      stdout: "ignore",
    },
    {
      command: `npm run build && npx vite preview --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      cwd: import.meta.dirname,
      url: `http://127.0.0.1:${WEB_PORT}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { ATLAS_API: `http://127.0.0.1:${API_PORT}` },
      stdout: "ignore",
    },
  ],
});
