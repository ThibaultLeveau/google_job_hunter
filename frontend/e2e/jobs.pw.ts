import type { Page } from '@playwright/test';
import type { components } from '../generated/api';
import { test, expect, signIn, signOut } from './fixtures.ts';
import { control } from './stack.ts';

type Detail = components['schemas']['JobDetail'];
type Run = components['schemas']['SearchRun'];
type Saved = components['schemas']['SavedSearch'];
type Artifacts = {
  public: Record<string, Record<string, string>[]>;
  tasks: { id: string; key: string; kind: string; status: string; attempts: number }[];
  profile_revision: number;
  matches: { id: string; job_id: string; generation: number; profile_revision: number; requirements_id: string; content_id: string; chunk_version: string; evaluator_version: string }[];
};
const query = '("Python" OR "Go") AND ("remote") AND (site:jobs.example.org)';
const artifactTables = ['job_sources', 'job_snapshots', 'job_preparations', 'job_cleanings', 'job_metadata', 'job_requirements', 'job_chunks', 'job_embeddings', 'job_embedding_spaces'];

async function addSkill(page: Page, skill: string): Promise<void> {
  await page.getByRole('textbox', { name: 'Skill name', exact: true }).fill(skill);
  await page.getByRole('button', { name: 'Add skill', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Skills', exact: true }).getByRole('heading', { name: skill, exact: true })).toBeVisible();
}

async function createSearch(page: Page, name: string): Promise<Saved> {
  await page.getByRole('link', { name: 'Searches', exact: true }).click();
  await page.getByRole('button', { name: 'New search', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search name', exact: true }).fill(name);
  await page.getByRole('textbox', { name: 'Group 1 term 1', exact: true }).fill('Python');
  await page.getByRole('button', { name: 'Add OR term to group 1', exact: true }).click();
  await page.getByRole('textbox', { name: 'Group 1 term 2', exact: true }).fill('Go');
  await page.getByRole('button', { name: 'Add AND group', exact: true }).click();
  await page.getByRole('textbox', { name: 'Group 2 term 1', exact: true }).fill('remote');
  await page.getByLabel('Search origin country', { exact: true }).selectOption('fr');
  await page.getByLabel('Result language', { exact: true }).selectOption('en');
  await page.getByRole('checkbox', { name: 'jobs.example.org', exact: true }).check();
  await expect(page.getByText(query, { exact: true })).toBeVisible();
  const saved = page.waitForResponse(response => response.url().endsWith('/api/v1/searches') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Save search', exact: true }).click();
  const response = await saved;
  expect(response.status()).toBe(201);
  const value = await response.json() as Saved;
  expect(value.definition).toMatchObject({ groups: [['Python', 'Go'], ['remote']], country: 'fr', language: 'en', sites: ['jobs.example.org'] });
  return value;
}

async function runSearch(page: Page, search: Saved, found: number, known = 0): Promise<Run> {
  await page.getByLabel('Saved search on this page', { exact: true }).selectOption(search.id);
  const started = page.waitForResponse(response => response.url().endsWith(`/searches/${search.id}/runs`) && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Run search', exact: true }).click();
  const response = await started;
  expect(response.status()).toBe(202);
  const run = await response.json() as Run;
  expect(run.snapshot).toMatchObject({ query, provider_parameters: { gl: 'fr', lr: 'lang_en' } });
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/searches/runs/${run.id}`);
    expect(response.status()).toBe(200);
    return (await response.json() as Run).status;
  }, { timeout: 45000 }).toBe('completed');
  await page.getByRole('button', { name: 'Refresh run history', exact: true }).click();
  await expect(page.getByText(`Found: ${String(found)} · New URLs: ${String(found - known)} · Existing URLs: ${String(known)} · Skipped: 0`, { exact: true })).toBeVisible();
  return run;
}

async function artifacts(owner: string): Promise<Artifacts> {
  return await control('jobs', '--owner', owner) as Artifacts;
}

async function ready(owner: string, attempts = 1): Promise<Artifacts> {
  await expect.poll(async () => {
    const state = await artifacts(owner);
    return state.tasks.length === 7 && state.tasks.every(task => task.status === 'succeeded') && state.matches.length === 1;
  }, { timeout: 60000 }).toBe(true);
  const state = await artifacts(owner);
  expect(Object.keys(state.public).sort()).toEqual([...artifactTables].sort());
  for (const rows of Object.values(state.public)) {
    expect(rows).toHaveLength(1);
    for (const value of Object.values(rows[0] ?? {})) expect(value).not.toBe('');
  }
  expect(new Set(state.tasks.map(task => task.key)).size).toBe(7);
  for (const task of state.tasks) expect(task.attempts).toBe(task.kind === 'job_crawl' ? attempts : 1);
  expect(state.matches[0]).toMatchObject({ job_id: state.public.job_sources?.[0]?.id, profile_revision: state.profile_revision,
    generation: 1, requirements_id: state.public.job_requirements?.[0]?.id, content_id: state.public.job_cleanings?.[0]?.id,
    chunk_version: state.public.job_chunks?.[0]?.policy_version });
  expect(state.matches[0]?.evaluator_version).toMatch(/\S+/);
  return state;
}

async function openJob(page: Page): Promise<string> {
  await page.getByRole('link', { name: 'Jobs', exact: true }).click();
  await page.getByRole('button', { name: 'Refresh jobs', exact: true }).click();
  const link = page.getByRole('link', { name: 'Python engineer', exact: true });
  await expect(link).toHaveCount(1);
  await link.click();
  await expect(page.getByRole('heading', { name: 'Job details', exact: true })).toBeVisible();
  return new URL(page.url()).pathname;
}

async function detail(page: Page, path: string): Promise<Detail> {
  const response = await page.request.get(`/api/v1${path}`);
  expect(response.status()).toBe(200);
  return await response.json() as Detail;
}

test.describe('duplicate and repeated discovery', () => {
  test.use({ scenario: 'duplicate' });
  test('accepted profile to cited match, preserved filters, reused artifacts and stale-profile recovery', async ({ page, stack }) => {
    test.setTimeout(180000);
    const user = stack.users[0];
    if (!user) throw new Error('Missing seeded owner.');
    await signIn(page, user);
    await addSkill(page, 'Python');
    const saved = await createSearch(page, 'Python or Go remote');
    const run = await runSearch(page, saved, 2, 1);
    const before = await ready(user.id);
    const path = await openJob(page);
    const data = await detail(page, path);
    expect(data.status).toMatchObject({ analysis_ready: true, content_ready: true, embeddings_ready: true, match_current: true });
    expect(data).toMatchObject({ preparation_id: before.public.job_preparations?.[0]?.id, content_id: before.public.job_cleanings?.[0]?.id,
      metadata_id: before.public.job_metadata?.[0]?.id, requirements_id: before.public.job_requirements?.[0]?.id });
    expect(data.source?.version).toBe(before.public.job_preparations?.[0]?.version);
    expect(data.clean_text?.version).toBe(before.public.job_cleanings?.[0]?.version);
    expect(data.metadata?.version).toBe(before.public.job_metadata?.[0]?.version);
    expect(data.requirements?.version).toBe(before.public.job_requirements?.[0]?.version);
    expect(data.match?.score).toMatchObject({ percent: 100, requirement_count: 1, unknown_count: 0 });
    expect(data.match?.evaluations[0]).toMatchObject({ status: 'matched', profile_evidence: [data.match_context?.profile_facts[0]?.id] });
    expect(Object.values(data.match_context?.citations ?? {}).map(span => span.quote)).toContain('Python required');
    const checklist = page.getByRole('region', { name: 'Application checklist', exact: true });
    await checklist.getByText('Full checklist and evidence (1 items)', { exact: true }).click();
    await expect(checklist.getByRole('heading', { name: 'Required', exact: true })).toBeVisible();
    await checklist.getByText('Source evidence (1 excerpts)', { exact: true }).click();
    await expect(checklist.getByRole('blockquote')).toHaveText('Python required');
    const match = page.getByRole('region', { name: 'Your profile match', exact: true });
    await expect(match.getByText('Current evaluation', { exact: true })).toBeVisible();
    await expect(match.getByText('100%', { exact: true })).toBeVisible();
    await match.getByText('Requirement assessments (1)', { exact: true }).click();
    await expect(match.getByRole('heading', { name: 'Matched (1)', exact: true })).toBeVisible();
    await match.getByRole('link', { name: 'Source excerpt 1', exact: true }).click();
    await expect(match.getByRole('blockquote')).toHaveText('Python required');
    await expect(match.locator('figure')).toBeFocused();
    await match.getByRole('link', { name: 'Python', exact: true }).click();
    await expect(match.locator('[id^="match-profile-"]')).toBeFocused();
    await page.getByRole('region', { name: 'Posting text', exact: true }).getByText('Read prepared posting text', { exact: true }).click();
    await expect(page.getByText('Python engineer\n\nSynthetic Company\n\nRemote\n\nPython required', { exact: true })).toBeVisible();

    await page.getByRole('link', { name: 'Back to jobs', exact: true }).click();
    await page.getByRole('textbox', { name: 'Keyword', exact: true }).fill('Python');
    await expect(page).toHaveURL(/keyword=Python/);
    await page.getByLabel('Search run', { exact: true }).selectOption(run.id);
    await page.getByLabel('Work arrangement filter', { exact: true }).selectOption('remote');
    await page.getByLabel('Match status', { exact: true }).selectOption('current');
    await page.getByLabel('Sort jobs', { exact: true }).selectOption('match');
    await expect(page).toHaveURL(/sort=match/);
    const filtered = page.url();
    await page.reload();
    await expect(page.getByLabel('Search run', { exact: true })).toHaveValue(run.id);
    await expect(page.getByLabel('Match status', { exact: true })).toHaveValue('current');
    await expect(page.getByRole('textbox', { name: 'Keyword', exact: true })).toHaveValue('Python');
    await page.getByRole('link', { name: 'Python engineer', exact: true }).click();
    await page.reload();
    await expect(match.getByText('Current evaluation', { exact: true })).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(filtered);
    await expect(page.getByRole('link', { name: 'Python engineer', exact: true })).toHaveCount(1);
    await page.getByRole('link', { name: 'Searches', exact: true }).click();
    const repeated = await runSearch(page, saved, 2, 2);
    expect(repeated.id).not.toBe(run.id);
    expect(await ready(user.id)).toEqual(before);

    await control('pause-worker');
    try {
      await page.getByRole('link', { name: 'Profile', exact: true }).click();
      await addSkill(page, 'Go');
      await page.goto(path);
      await expect(match.getByText('Outdated result', { exact: true })).toBeVisible();
      await expect(match.getByText('Your accepted profile changed after this evaluation. The evidence below belongs to the earlier profile.', { exact: true })).toBeVisible();
      const stale = await detail(page, path);
      expect(stale.match?.profile_revision).toBe(before.profile_revision);
      expect(stale.match_context?.profile_facts.map(fact => fact.skill)).toEqual(['Python']);
      expect((await artifacts(user.id)).matches).toEqual([]);
    } finally { await control('resume-worker'); }
    const refreshed = await ready(user.id);
    expect(refreshed.profile_revision).toBe(before.profile_revision + 1);
    expect(refreshed.matches[0]?.id).not.toBe(before.matches[0]?.id);
    expect(refreshed.public).toEqual(before.public);
    expect(refreshed.tasks).toEqual(before.tasks);
    await page.getByRole('button', { name: 'Refresh details', exact: true }).click();
    await expect(match.getByText('Current evaluation', { exact: true })).toBeVisible();
    expect((await detail(page, path)).match?.profile_revision).toBe(refreshed.profile_revision);
  });
});

test('private profile, search, run and match isolation with shared public analysis', async ({ page, stack }) => {
  test.setTimeout(150000);
  const alice = stack.users[0], bob = stack.users[1];
  if (!alice || !bob) throw new Error('Missing seeded owners.');
  await signIn(page, alice);
  await addSkill(page, 'Python');
  const search = await createSearch(page, 'Alice private search');
  const run = await runSearch(page, search, 1);
  const aliceState = await ready(alice.id);
  const path = await openJob(page);
  const aliceDetail = await detail(page, path);
  const fact = aliceDetail.match_context?.profile_facts[0]?.id;
  expect(fact).toBeTruthy();
  await signOut(page);
  await signIn(page, bob);
  await expect(page.getByRole('region', { name: 'Skills', exact: true }).getByRole('heading', { name: 'Python', exact: true })).toHaveCount(0);
  const profile = await page.request.get('/api/v1/profile');
  expect(await profile.json()).toMatchObject({ facts: [] });
  for (const resource of [`/api/v1${path}`, `/api/v1/searches/${search.id}`, `/api/v1/searches/runs/${run.id}`, `/api/v1/profile/facts/${fact ?? ''}`]) {
    const response = await page.request.get(resource);
    expect(response.status()).toBe(404);
    expect(await response.text()).not.toContain(alice.id);
  }
  await page.goto(path);
  await expect(page.getByRole('heading', { name: 'Job unavailable', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Your profile match', exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: 'Jobs', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'No discoveries yet', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Searches', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'No saved searches yet', exact: true })).toBeVisible();
  await expect(page.getByText('Alice private search', { exact: true })).toHaveCount(0);
  const own = await createSearch(page, 'Bob private search');
  await runSearch(page, own, 1, 1);
  const bobState = await ready(bob.id);
  expect(bobState.public).toEqual(aliceState.public);
  expect(bobState.tasks).toEqual(aliceState.tasks);
  expect(bobState.matches[0]?.id).not.toBe(aliceState.matches[0]?.id);
  expect(await openJob(page)).toBe(path);
  const bobDetail = await detail(page, path);
  expect(bobDetail.match?.score.percent).toBeNull();
  expect(bobDetail.match_context?.profile_facts).toEqual([]);
  expect(bobDetail.match?.evaluations[0]?.status).toBe('unknown');
  expect(JSON.stringify(bobDetail)).not.toContain(fact);
  expect(JSON.stringify(bobDetail)).not.toContain(aliceState.matches[0]?.id);
  await expect(page.getByRole('region', { name: 'Your profile match', exact: true }).getByText('Score unknown', { exact: true })).toBeVisible();
  expect(await artifacts(alice.id)).toEqual(aliceState);
});

test.describe('durable retry', () => {
  test.use({ scenario: 'transient' });
  test('a crawl 503 resumes the same task and publishes one complete artifact chain', async ({ page, stack }) => {
    const user = stack.users[0];
    if (!user) throw new Error('Missing seeded owner.');
    await signIn(page, user);
    await addSkill(page, 'Python');
    const saved = await createSearch(page, 'Retryable crawl');
    await runSearch(page, saved, 1);
    await ready(user.id, 2);
    const path = await openJob(page);
    const data = await detail(page, path);
    expect(data.status.analysis_ready).toBe(true);
    expect(data.match?.score.percent).toBe(100);
    await expect(page.getByRole('region', { name: 'Your profile match', exact: true }).getByText('Current evaluation', { exact: true })).toBeVisible();
  });
});
