import { execFile, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';
import { preview, type PreviewServer } from 'vite';

export const frontend = resolve(import.meta.dirname, '..');
export const python = resolve(frontend, '../backend/.venv/bin/python');
const harness = resolve(frontend, '../backend/tests/stack_harness.py');
const execute = promisify(execFile);
export type Scenario = 'success' | 'duplicate' | 'transient' | 'owner-isolation';
export interface StackState {
  root: string;
  api_url: string;
  users: { id: string; email: string; password: string }[];
}

export function statePath(): string {
  const path = process.env.E2E_STATE;
  if (!path) throw new Error('Playwright must supply a private E2E_STATE path.');
  return path;
}

export async function readState(): Promise<StackState> {
  return JSON.parse(await readFile(statePath(), 'utf8')) as StackState;
}

export async function control(action: 'reset' | 'seed' | 'mail' | 'authorize' | 'resume' | 'expire' | 'dedup', ...args: string[]): Promise<unknown> {
  const result = await execute(python, [harness, action, '--state', statePath(), ...args], { timeout: 75000 });
  return JSON.parse(result.stdout) as unknown;
}

async function serve(): Promise<void> {
  if (existsSync(statePath())) throw new Error('Use a new private state path.');
  const port = Number(process.env.E2E_PORT ?? 4175);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid E2E_PORT.');
  const origin = `http://127.0.0.1:${String(port)}`;
  const abort = new AbortController();
  const shutdown = new AbortController();
  const aborted = () => abort.signal.aborted;
  const stop = () => { shutdown.abort(); abort.abort(); };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
  const child = spawn(python, [harness, 'start', '--mode', 'test', '--state', statePath(), '--frontend-url', origin], {
    cwd: frontend, stdio: ['ignore', 'ignore', 'pipe'],
  });
  child.stderr.pipe(process.stderr);
  const exited = new Promise<void>(done => {
    child.once('error', () => { abort.abort(); done(); });
    child.once('exit', () => { abort.abort(); done(); });
  });
  const closeHarness = async () => {
    if (!child.killed) child.kill('SIGTERM');
    await Promise.race([
      exited,
      delay(90000, undefined, { ref: false }).then(() => { child.kill('SIGKILL'); throw new Error('Harness teardown deadline exceeded.'); }),
    ]);
  };
  let server: PreviewServer | undefined;
  try {
    const deadline = Date.now() + 90000;
    let state: StackState | undefined;
    while (!state && Date.now() < deadline && !abort.signal.aborted) {
      try { state = await readState(); } catch { await delay(100, undefined, { signal: abort.signal }); }
    }
    if (!state || abort.signal.aborted) throw new Error('Real stack failed to start.');
    server = await preview({
      configFile: resolve(frontend, 'vite.config.ts'),
      // Vite's signal handler exits after its close hooks complete.
      plugins: [{ name: 'test-stack-cleanup', closePreviewServer: closeHarness }],
      preview: { host: '127.0.0.1', port, strictPort: true, proxy: { '/api': { target: state.api_url, changeOrigin: false } } },
    });
    if (process.env.E2E_PROBE_RECORD) {
      const config = JSON.parse(await readFile(resolve(state.root, 'config.json'), 'utf8')) as { database_url: string };
      await writeFile(process.env.E2E_PROBE_RECORD, JSON.stringify({
        root: state.root, api: state.api_url, database: new URL(config.database_url).pathname.slice(1),
      }), { mode: 0o600 });
    }
    if (!aborted()) await new Promise<void>(done => { abort.signal.addEventListener('abort', () => { done(); }, { once: true }); });
    if (!shutdown.signal.aborted) throw new Error('Real stack exited unexpectedly.');
  } finally {
    try {
      await server?.close();
    } finally {
      await closeHarness();
      process.off('SIGTERM', stop);
      process.off('SIGINT', stop);
    }
  }
}

if (import.meta.main) {
  serve().catch(() => { console.error('Browser stack failed; check local PostgreSQL, the build and test settings.'); process.exitCode = 1; });
}
