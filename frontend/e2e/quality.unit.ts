import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { test } from 'node:test';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
mkdirSync(join(frontend, 'reports'), { recursive: true });

for (const probe of ['control', 'line-drop', 'branch-drop', 'caught-network'] as const) {
  await test(`quality gate ${probe}: expected outcome and failure reason`, () => {
    const root = mkdtempSync(join(frontend, 'reports/quality-probe-'));
    const source = join(root, 'src');
    mkdirSync(source);
    try {
      writeFileSync(join(root, 'vite.config.mjs'), `
import base from ${JSON.stringify(pathToFileURL(join(frontend, 'vite.config.ts')).href)};
export default { ...base, root: ${JSON.stringify(root)}, test: {
  ...base.test,
  setupFiles: [${JSON.stringify(join(frontend, 'src/test/setup.ts'))}],
  outputFile: { junit: ${JSON.stringify(join(root, 'junit.xml'))} },
  coverage: { ...base.test.coverage, reportsDirectory: ${JSON.stringify(join(root, 'coverage'))} },
}};
`);
      writeFileSync(join(source, 'subject.ts'), 'export function choose(value: boolean) { return value ? 1 : 2; }\n');
      if (probe === 'line-drop') {
        // This maintained-shaped module is never imported: inclusion must still lower coverage.
        writeFileSync(join(source, 'unvisited.ts'), Array.from({ length: 20 }, (_, index) => `export const value${String(index)} = ${String(index)};`).join('\n'));
      }
      writeFileSync(join(source, 'probe.test.ts'), `
import { expect, test } from 'vitest';
import { choose } from './subject';
test('synthetic gate probe', async () => {
  expect(choose(true)).toBe(1);
  ${probe === 'branch-drop' ? '' : 'expect(choose(false)).toBe(2);'}
  ${probe === 'caught-network' ? "try { await fetch('http://127.0.0.1:54321/unhandled-quality-probe'); } catch { /* deliberately swallowed */ }" : ''}
});
`);
      const result = spawnSync(process.execPath, [
        join(frontend, 'node_modules/vitest/vitest.mjs'), 'run', '--config', join(root, 'vite.config.mjs'), '--coverage',
      ], { cwd: frontend, encoding: 'utf8', timeout: 60000 });
      assert.ifError(result.error);
      const output = result.stdout + result.stderr;
      assert.equal(result.status, probe === 'control' ? 0 : 1, output);
      const lineFailure = /Coverage for lines .* does not meet global threshold \(90%\)/;
      const branchFailure = /Coverage for branches .* does not meet global threshold \(85%\)/;
      if (probe === 'line-drop') assert.match(output, lineFailure);
      else assert.doesNotMatch(output, lineFailure);
      if (probe === 'branch-drop') assert.match(output, branchFailure);
      else assert.doesNotMatch(output, branchFailure);
      if (probe === 'caught-network') assert.match(output, /Unhandled network requests/);
      else assert.doesNotMatch(output, /Unhandled network requests/);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
}
