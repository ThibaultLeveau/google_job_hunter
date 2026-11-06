import { randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { defineConfig, devices } from '@playwright/test';

process.env.E2E_STATE ??= join(tmpdir(), `job-hunter-browser-${randomUUID()}.json`);
const baseURL = `http://127.0.0.1:${process.env.E2E_PORT ?? '4175'}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.pw.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: 90000,
  globalTimeout: 600000,
  expect: { timeout: 10000 },
  outputDir: 'reports/browser/results',
  reporter: [['list'], ['html', { outputFolder: 'reports/browser/html', open: 'never' }], ['junit', { outputFile: 'reports/browser/junit.xml' }]],
  use: { baseURL, serviceWorkers: 'block', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // Keep the supervised PID alive until the harness finishes cleanup.
    command: 'exec node e2e/stack.ts',
    url: `${baseURL}/api/health/ready`,
    reuseExistingServer: false,
    timeout: 120000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 120000 },
    stdout: 'ignore',
    stderr: 'pipe',
  },
});
