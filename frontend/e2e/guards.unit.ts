import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { frontend, python } from './stack.ts';

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>(done => { server.listen(0, '127.0.0.1', done); });
  const address = server.address();
  assert(address && typeof address === 'object');
  await new Promise<void>((done, reject) => { server.close(error => { if (error) reject(error); else done(); }); });
  return address.port;
}

for (const probe of ['outbound', 'failure', 'production'] as const) {
  await test(`${probe} fails and cleans up the real stack`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'job-hunter-browser-probe-'));
    const state = join(directory, 'state.json');
    const record = join(directory, 'record.json');
    const env: NodeJS.ProcessEnv = { ...process.env, E2E_STATE: state, E2E_PORT: String(await freePort()), E2E_PROBE: probe, E2E_PROBE_RECORD: record };
    if (probe === 'production') env.JOB_HUNTER_AI_ENVIRONMENT = 'production';
    try {
      if (probe === 'production') {
        const assets = resolve(frontend, 'dist/assets');
        for (const name of readdirSync(assets).filter(name => name.endsWith('.js'))) {
          assert.doesNotMatch(readFileSync(join(assets, name), 'utf8'), /E2E_STATE|fixture-secret|Synthetic-only passphrase|Unexpected external request/);
        }
      }
      const args = probe === 'production' ? ['e2e/stack.ts']
        : ['node_modules/@playwright/test/cli.js', 'test', '--reporter=line', '--output', join(directory, 'results')];
      const result = spawnSync(process.execPath, args, { cwd: frontend, env, encoding: 'utf8', timeout: 240000 });
      assert.ifError(result.error);
      assert.equal(result.status, 1, result.stdout + result.stderr);
      assert.match(result.stdout + result.stderr, probe === 'outbound' ? /Unexpected external request/
        : probe === 'failure' ? /Intentional failure after readiness/ : /verify test mode/);
      assert(!existsSync(state), 'Harness state must be removed after failure');
      if (probe !== 'production') {
        const saved = JSON.parse(readFileSync(record, 'utf8')) as { root: string; api: string; database: string };
        assert(!existsSync(saved.root), 'Private files must be removed after failure');
        await assert.rejects(fetch(saved.api + '/api/health/live', { signal: AbortSignal.timeout(1000) }));
        const code = 'import os; from sqlalchemy import create_engine,text; e=create_engine(os.environ["TEST_DATABASE_URL"]); c=e.connect(); assert c.scalar(text("SELECT count(*) FROM pg_database WHERE datname=:name"), {"name":os.environ["E2E_DATABASE"]}) == 0; c.close(); e.dispose()';
        execFileSync(python, ['-c', code], { env: { ...process.env, E2E_DATABASE: saved.database }, timeout: 10000 });
      }
    } finally {
      if (existsSync(state)) execFileSync(python, [resolve(frontend, '../backend/tests/stack_harness.py'), 'stop', '--state', state], { timeout: 95000 });
      rmSync(directory, { recursive: true, force: true });
    }
  });
}
