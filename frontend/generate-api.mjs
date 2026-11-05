import { execFileSync } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import openapiTS from 'openapi-typescript';

// Export the real routes with no operator credentials or database configuration.
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('JOB_HUNTER_')));
const schema = execFileSync('uv', ['run', '--offline', '--directory', '../backend', '--locked', 'python', '-c', `
import json
from job_hunter.app import create_app
from job_hunter.settings import Settings
print(json.dumps(create_app(Settings(database_url=None)).openapi(), sort_keys=True, indent=2))
`], { env, encoding: 'utf8', timeout: 60000, maxBuffer: 8 * 1024 * 1024 });
const types = await openapiTS(JSON.parse(schema), {
  alphabetize: true,
  fetch: () => { throw new Error('External schema references are not allowed.'); },
});
await mkdir('generated', { recursive: true });
await writeFile('generated/openapi.json', schema);
await writeFile('generated/api.d.ts', types);
