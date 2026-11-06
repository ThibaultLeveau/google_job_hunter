import { focusManager, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { components } from '../../../generated/api';
import App from '../../App';
import { api, sessionKey, type Session } from '../../shared/api';
import { server } from '../../test/setup';

type Detail = components['schemas']['JobDetail'];
const owner: Session = { id: '00000000-0000-4000-8000-000000000001', email: 'reader@example.org', email_verified: true, csrf_token: 'c'.repeat(64) };
const id = '12345678-1234-4000-8000-123456789abc', snapshot = '12345678-1234-4000-8000-123456789def', now = '2026-01-01T10:00:00Z', hash = 'a'.repeat(64);
const path = `http://localhost/api/v1/jobs/${id}`;
let current: Session | null, data: Detail;
function fixture(): Detail {
  const lines = ['Senior engineer', 'Example company', 'Paris', 'France', 'remote', 'EUR 50000 to 70000 per year gross', 'Python required', 'Portfolio preferred', 'Contact our recruiting team', 'Apply before 2026-02-01T10:00:00Z'];
  let start = 0;
  const paragraphs = lines.map(text => { const value = { id: crypto.randomUUID(), start, end: start + text.length, text, content_hash: hash }; start = value.end + 2; return value; });
  function cite(index: number): components['schemas']['Citation'] { const p = paragraphs[index]; if (!p) throw new Error('Missing fixture paragraph'); return { paragraph_id: p.id, start: p.start, end: p.end, quote: p.text }; }
  const facts: components['schemas']['Fact'][] = (['title', 'employer', 'location', 'region', 'work_arrangement'] as const).map((field, index) => ({ field, value: lines[index] ?? '', evidence: cite(index), uncertain: false }));
  const requirements: components['schemas']['Requirement'][] = ([['job_requirement', 'required', 6], ['material', 'preferred', 7], ['instruction', 'unclear', 8], ['deadline', 'required', 9]] as const)
    .map(([kind, level, index]) => ({ id: crypto.randomUUID(), kind, level, text: lines[index] ?? '', evidence: [cite(index)], deadline_at: kind === 'deadline' ? '2026-02-01T10:00:00Z' : null }));
  return { status: { id, snapshot_id: snapshot, freshness: 'current', content_ready: true, analysis_ready: true, embeddings_ready: true, stages: [], match_state: 'current', match_current: true, match_stale: false, match_pending: false, match_reason: null, retries_remaining: 5, reevaluations_remaining: 3 },
    source_url: 'https://jobs.example.org/position', fetched_at: now, preparation_id: crypto.randomUUID(), content_id: crypto.randomUUID(), metadata_id: crypto.randomUUID(), requirements_id: crypto.randomUUID(), match: null,
    source: { snapshot_id: snapshot, source_hash: hash, content_hash: hash, version: 'source-v1', language: 'fr', state: 'uncertain', paragraphs },
    clean_text: { snapshot_id: snapshot, source_hash: hash, content_hash: hash, version: hash, state: 'cleaned', text: lines.join('\n\n'), mappings: [], removed: [] },
    metadata: { snapshot_id: snapshot, source_hash: hash, version: 'metadata-v1', source_language: 'fr', state: 'ready', reason: null, work_arrangement: 'remote', salary_status: 'known',
      fields: Object.fromEntries(facts.map(value => [value.field, { status: 'known', values: [value] }])),
      selection: { kind: 'job', evidence: [cite(0)], facts, salaries: [{ minimum: '50000', maximum: '70000', currency: 'EUR', period: 'year', basis: 'gross', uncertain: false, evidence: cite(5) }] } },
    requirements: { metadata_id: crypto.randomUUID(), snapshot_id: snapshot, source_hash: hash, version: 'requirements-v1', state: 'ready', reason: null, items: requirements,
      summary: 'Required: Python required\nPreferred: Portfolio preferred', more_requirements: 2 },
  };
}
const failure = (status: number) => HttpResponse.json({ code: 'fixture', message: 'Private failure detail', field_errors: [], request_id: owner.id }, { status });
function gate() { let release: () => void = () => undefined; const pending = new Promise<void>(resolve => { release = resolve; }); return { pending, release }; }
function mount(route = `/jobs/${id}`, state: unknown = null) {
  window.history.replaceState({ usr: state }, '', route); api.acceptSession(current);
  return render(<QueryClientProvider client={api.queryClient}><App /></QueryClientProvider>);
}
const scroll = vi.fn();
const button = (name: string) => screen.getByRole('button', { name });
beforeEach(() => {
  api.clearSession(); api.queryClient.clear(); current = owner; data = fixture(); api.queryClient.setQueryDefaults(sessionKey, { retry: false });
  scroll.mockClear(); vi.spyOn(window, 'scrollTo').mockImplementation(scroll);
  server.use(http.get('http://localhost/api/auth/me', () => current ? HttpResponse.json(current) : failure(401)),
    http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: false })), http.get(path, () => HttpResponse.json(data)),
    http.get(path + '/status', () => HttpResponse.json(data.status)));
});
afterEach(() => { vi.useRealTimers(); focusManager.setFocused(undefined); api.clearSession(); api.queryClient.clear(); });

test('opens a deep link with source facts, original salary units and safe posting navigation', async () => {
  mount(`/jobs/${id.toUpperCase()}`); expect(await screen.findByRole('heading', { name: 'Senior engineer' })).toBeVisible();
  expect(document.title).toBe('Job details · Job Hunter'); expect(screen.getByRole('link', { name: 'Back to jobs' })).toHaveAttribute('href', '/jobs');
  expect(within(document.querySelector('.job-detail-heading') as HTMLElement).getByText('Example company')).toBeVisible(); expect(screen.getByText('Paris', { selector: 'dd' })).toBeVisible(); expect(screen.getByText('Remote')).toBeVisible();
  expect(screen.getByText('EUR 50000–70000 / year · gross')).toBeVisible(); expect(screen.getByText(/Source language: fr/)).toBeVisible();
  expect(screen.getByText('2026-01-01 10:00 UTC')).toHaveAttribute('datetime', now);
  const link = screen.getByRole('link', { name: /Original posting at jobs.example.org.*opens in a new tab/ });
  expect(link).toHaveAttribute('href', data.source_url); expect(link).toHaveAttribute('target', '_blank'); expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  expect(screen.getByText('A profile evaluation is available.')).toBeVisible(); expect(screen.queryByText(/80%/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /retry.*stage/i })).not.toBeInTheDocument();
  expect(button('Reevaluate profile match')).toBeEnabled();
});

test('expands evidence and full text with native disclosures and separates requirement classes', async () => {
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' });
  const checklist = screen.getByText('Full checklist and evidence (4 items)'); await user.click(checklist);
  expect(checklist.closest('details')).toHaveAttribute('open');
  for (const name of ['Required', 'Preferred', 'Priority unclear']) expect(screen.getByRole('heading', { name })).toBeVisible();
  for (const name of ['Job requirement', 'Application material', 'Application instruction', 'Deadline']) expect(screen.getByText(name, { exact: true })).toBeVisible();
  expect(screen.getByText('2026-02-01 10:00 UTC')).toHaveAttribute('datetime', '2026-02-01T10:00:00Z');
  const evidence = within(screen.getByRole('region', { name: 'Required' })).getAllByText('Source evidence (1 excerpts)')[0];
  if (!evidence) throw new Error('Missing evidence disclosure'); expect(screen.queryByText(/Source paragraph 7 ·/)).not.toBeVisible();
  await user.click(evidence); expect(screen.getByText(/Source paragraph 7 ·/)).toBeVisible();
  expect(within(evidence.closest('details') as HTMLElement).getByText('Python required')).toBeVisible();
  const facts = screen.getByText('Evidence for job facts (6 excerpts)'); await user.click(facts); expect(screen.getByText(/Source paragraph 1 ·/)).toBeVisible();
  await user.click(screen.getByText('Read prepared posting text')); expect(screen.getByText('Read prepared posting text').closest('details')).toHaveTextContent('Senior engineer Example company Paris France');
  await user.click(screen.getByText('Read preserved source (10 paragraphs)')); expect(screen.getByRole('heading', { name: 'Paragraph 10' })).toBeVisible();
  await user.click(checklist); expect(checklist.closest('details')).not.toHaveAttribute('open');
});

test('restores the filtered list URL, cursor history and scroll position using the back link', async () => {
  api.queryClient.setQueryData(['jobs-position', owner.id, '?keyword=Python&cursor=third'], 480);
  const user = userEvent.setup(); mount(`/jobs/${id}`, { returnTo: '/jobs?keyword=Python&cursor=third', jobCursors: ['', 'second', 42] });
  api.queryClient.setQueryData(['jobs-position', owner.id, '?keyword=Python&cursor=third'], 480);
  await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(screen.getByRole('link', { name: 'Back to jobs' }));
  expect(window.location.pathname + window.location.search).toBe('/jobs?keyword=Python&cursor=third');
  expect(window.history.state).toMatchObject({ usr: { jobCursors: ['', 'second'] } });
  await waitFor(() => { expect(scroll).toHaveBeenCalledWith({ top: 480, behavior: 'instant' }); });
});

test.each(['https://outside.example/jobs', '//outside.example/jobs', '/profile', '/jobs/other', '/jobs\\evil', '/jobs?x=' + 'a'.repeat(2050)])('rejects unsafe or unrelated list context %s', async returnTo => {
  mount(`/jobs/${id}`, { returnTo }); await screen.findByRole('heading', { name: 'Senior engineer' }); expect(screen.getByRole('link', { name: 'Back to jobs' })).toHaveAttribute('href', '/jobs');
});

test('shows a bounded loading state while retaining an exit to the list', async () => {
  const deferred = gate(); server.use(http.get(path, async () => { await deferred.pending; return HttpResponse.json(data); }));
  mount(); expect(await screen.findByText('Loading job details')).toBeVisible(); expect(screen.getByRole('link', { name: 'Back to jobs' })).toBeVisible();
  expect(document.querySelector('.jobs-loading')).toHaveAttribute('aria-busy', 'true'); deferred.release(); expect(await screen.findByRole('heading', { name: 'Senior engineer' })).toBeVisible();
});

test.each([404, 422])('treats inaccessible or invalid jobs as unavailable without exposing payloads (%i)', async status => {
  server.use(http.get(path, () => failure(status))); mount(); expect(await screen.findByText('Job unavailable')).toBeVisible(); expect(screen.queryByText('Private failure detail')).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Senior engineer' })).not.toBeInTheDocument(); expect(screen.getByRole('link', { name: 'Back to jobs' })).toBeVisible();
});

test('rejects a malformed job ID without a detail request', async () => {
  mount('/jobs/not-an-id'); expect(await screen.findByText('Job unavailable')).toBeVisible();
});

test.each([0, 503])('recovers from detail loading failure %i', async status => {
  server.use(http.get(path, () => status ? failure(status) : HttpResponse.error())); const user = userEvent.setup(); mount();
  expect(await screen.findByText('Job details could not be loaded')).toBeVisible(); server.use(http.get(path, () => HttpResponse.json(data)));
  await user.click(button('Retry job details')); expect(await screen.findByRole('heading', { name: 'Senior engineer' })).toBeVisible();
});

test('keeps available content during refresh failure, blocks duplicate refresh and recovers', async () => {
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' });
  server.use(http.get(path, () => failure(503))); await user.click(button('Refresh details')); expect(await screen.findByRole('alert')).toHaveTextContent('Refresh failed');
  expect(screen.getByRole('heading', { name: 'Senior engineer' })).toBeVisible(); const deferred = gate(); let reads = 0;
  server.use(http.get(path, async () => { reads++; await deferred.pending; return HttpResponse.json(data); }));
  await user.click(button('Refresh details')); expect(button('Refreshing details…')).toBeDisabled(); deferred.release(); await waitFor(() => { expect(button('Refresh details')).toBeEnabled(); }); expect(reads).toBe(1);
});

test('preserves explicit unknowns, missing salary and incomplete processing without inventing facts', async () => {
  data = { ...data, metadata: null, source: null, clean_text: null, requirements: null, fetched_at: null,
    status: { ...data.status, freshness: 'pending', content_ready: false, analysis_ready: false, embeddings_ready: false, match_current: false, match_state: 'pending', match_pending: true } };
  mount(); await screen.findByRole('heading', { name: 'Job title unknown' }); expect(screen.getByText('Company unknown')).toBeVisible(); expect(screen.getByText('Salary unknown')).toBeVisible();
  expect(screen.getByText(/Not fetched yet.*Source language: Unknown/)).toBeVisible(); expect(screen.getByText(/Some details are still processing/)).toBeVisible();
  expect(screen.getByText(/Application requirements are not ready yet/)).toBeVisible(); expect(screen.getByText('Preserved source text is unavailable.')).toBeVisible(); expect(screen.getByText('Profile evaluation is pending.')).toBeVisible();
});

test.each(['unknown', 'unavailable', 'failed'] as const)('distinguishes requirements state %s from an empty success', async state => {
  if (!data.requirements) throw new Error('Missing requirements'); data.requirements = { ...data.requirements, state, items: [], summary: '', more_requirements: 0 };
  mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); expect(screen.getByText(state === 'unknown' ? /requirements are unknown/ : state === 'unavailable' ? /requirements are unavailable/ : /requirements could not be prepared/)).toBeVisible();
});

test.each(['stale', 'removed'] as const)('keeps preserved facts visible when source freshness is %s', async freshness => {
  data.status = { ...data.status, freshness, match_stale: true, match_current: false }; mount(); await screen.findByRole('heading', { name: 'Senior engineer' });
  expect(screen.getByText(freshness === 'stale' ? /Source content is stale/ : /original posting was removed/)).toBeVisible(); expect(screen.getByText('An earlier profile evaluation is out of date.')).toBeVisible();
});

test('distinguishes unsupported content, failed stages, non-job and uncertain metadata', async () => {
  if (!data.source || !data.metadata || !data.clean_text) throw new Error('Missing fixture artifacts');
  data.source = { ...data.source, state: 'unsupported', paragraphs: [] }; data.metadata = { ...data.metadata, state: 'ambiguous' }; data.clean_text = { ...data.clean_text, state: 'degraded' };
  data.status = { ...data.status, match_state: 'failed', match_current: false, analysis_ready: false, stages: [{ stage: 'fetch', revision: 'v1', generation: 1, state: 'unsupported', reason: 'http_denied', created_at: now, updated_at: now, retryable: false }] };
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); expect(screen.getByText(/blocked or unsupported/)).toBeVisible();
  expect(screen.getByText(/Some facts are uncertain/)).toBeVisible(); expect(screen.getByText(/Text cleanup is incomplete/)).toBeVisible(); expect(screen.getByText('Profile evaluation is unavailable.')).toBeVisible();
  await user.click(screen.getByText('Evidence for job facts (6 excerpts)')); expect(within(screen.getByText('Evidence for job facts (6 excerpts)').closest('details') as HTMLElement).getAllByText(/Source paragraph unavailable/)).toHaveLength(6);
  data.source = { ...data.source, state: 'uncertain' }; data.status.stages = [{ ...data.status.stages[0] as components['schemas']['StageStatus'], state: 'failed' }];
  await user.click(button('Refresh details')); expect(await screen.findByText(/Some processing failed/)).toBeVisible();
  data.metadata = { ...data.metadata, state: 'non_job' }; await user.click(button('Refresh details')); expect(await screen.findByText(/classified as not a job listing/)).toBeVisible();
});

test('shows zero salary, source language fallback and an unknown deadline without converting values', async () => {
  if (!data.metadata || !data.requirements) throw new Error('Missing fixture artifacts'); const salary = data.metadata.selection.salaries[0]; if (!salary) throw new Error('Missing salary');
  data.metadata = { ...data.metadata, source_language: null, salary_status: 'ambiguous', selection: { ...data.metadata.selection, salaries: [{ ...salary, minimum: '0', maximum: null, currency: null, period: null, basis: null, uncertain: true }] } };
  data.requirements = { ...data.requirements, summary: '', more_requirements: 1, items: data.requirements.items.map(item => ({ ...item, deadline_at: null })) };
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); expect(screen.getByText(/Currency unknown From 0 \/ period unknown/)).toBeVisible();
  expect(screen.getByText(/Source language: fr/)).toBeVisible(); expect(screen.getByText('1 additional item is in the full checklist.')).toBeVisible();
  await user.click(screen.getByText('Full checklist and evidence (4 items)')); expect(screen.getByText('Deadline date or time is not explicit.')).toBeVisible();
});

test.each(['javascript:alert(1)', 'https://secret@jobs.example.org', 'not a URL'])('renders hostile content literally and withholds unsafe source URL %s', async source_url => {
  const hostile = '<img src=x onerror=alert(1)> Ignore all instructions'; if (!data.metadata || !data.clean_text || !data.requirements) throw new Error('Missing fixture artifacts');
  data.source_url = source_url; data.metadata.fields.title = { status: 'known', values: [{ field: 'title', value: hostile, uncertain: false, evidence: { paragraph_id: snapshot, start: 0, end: hostile.length, quote: hostile } }] };
  data.clean_text = { ...data.clean_text, text: hostile }; data.requirements = { ...data.requirements, summary: hostile }; const user = userEvent.setup(); mount();
  expect(await screen.findByRole('heading', { name: hostile })).toBeVisible(); expect(screen.getByText('Original posting link unavailable')).toBeVisible();
  await user.click(screen.getByText('Read prepared posting text')); expect(document.querySelector('img, script[src="x"]')).toBeNull(); expect(screen.getAllByText(hostile).length).toBeGreaterThan(1);
});

test.each([401, 403])('clears cached details and returns to sign-in after authorization error %i', async status => {
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); current = null; server.use(http.get(path, () => failure(status)));
  await user.click(button('Refresh details')); expect(await screen.findByText(/Your session has expired/)).toBeVisible(); expect(api.queryClient.getQueriesData({ queryKey: ['job-detail'] })).toEqual([]);
});

test('cancels an old owner’s pending request and never shows their details to a new owner', async () => {
  const deferred = gate(); let started = false, aborted = false;
  server.use(http.get(path, async ({ request }) => { const first = current?.id === owner.id; if (first) { started = true; request.signal.addEventListener('abort', () => { aborted = true; }); await deferred.pending; }
    return first ? HttpResponse.json(data) : failure(404); }));
  mount(); await waitFor(() => { expect(started).toBe(true); }); act(() => { current = { ...owner, id: snapshot }; api.acceptSession(current); });
  expect(aborted).toBe(true); expect(await screen.findByText('Job unavailable')).toBeVisible(); deferred.release(); await act(async () => { await deferred.pending; });
  expect(screen.queryByRole('heading', { name: 'Senior engineer' })).not.toBeInTheDocument(); expect(api.queryClient.getQueriesData({ queryKey: ['job-detail', owner.id] })).toEqual([]);
});


test('does not present terminal removed content as pending processing', async () => {
  data = { ...data, source: null, clean_text: null, requirements: null, metadata: null, status: { ...data.status, freshness: 'removed', analysis_ready: false, match_current: false } };
  mount(); await screen.findByRole('heading', { name: 'Job title unknown' });
  expect(screen.getByText('Application requirements are unavailable for this source.')).toBeVisible(); expect(screen.getByText('Prepared posting text is unavailable.')).toBeVisible();
  expect(screen.queryByText(/requirements are not ready yet/)).not.toBeInTheDocument(); expect(screen.queryByText(/Some details are still processing/)).not.toBeInTheDocument(); expect(screen.getByText('Profile evaluation is unavailable.')).toBeVisible();
});

function withMatch(): Detail {
  const value = fixture(); if (!value.requirements || !value.source || !value.requirements_id || !value.content_id) throw new Error('Missing match fixture');
  const facts: components['schemas']['EvaluatedProfileFact'][] = [{ id: crypto.randomUUID(), kind: 'skill', skill: 'Python' },
    { id: crypto.randomUUID(), kind: 'experience', title: 'Software engineer', description: 'Used TypeScript' }, { id: crypto.randomUUID(), kind: 'project', description: 'Built an API' }];
  const citations: components['schemas']['MatchContext']['citations'] = {};
  const items = value.requirements.items.map((item, index) => ({ ...item, kind: index === 3 ? 'material' as const : 'job_requirement' as const }));
  const evaluations: components['schemas']['Evaluation'][] = items.map((item, index) => {
    const citation = crypto.randomUUID(), span = item.evidence[0]; if (!span) throw new Error('Missing citation'); citations[citation] = span;
    return { requirement_id: item.id, status: (['matched', 'partial', 'missing', 'unknown'] as const)[index] ?? 'unknown', requirement_evidence: [citation],
      profile_evidence: index === 0 ? [facts[0]?.id ?? ''] : index === 1 ? facts.slice(1).map(fact => fact.id) : [], rationale: `Evidence explanation ${String(index + 1)}` };
  });
  value.match_context = { profile_changed: false, profile_facts: facts, source: value.source, requirements: { ...value.requirements, items }, citations };
  value.match = { owner: owner.id, profile_revision: 4, job_id: id, snapshot_id: snapshot, source_hash: hash, requirements_id: value.requirements_id,
    requirements_version: 'requirements-v1', content_id: value.content_id, chunk_version: 'chunks-v1', embedding_space: snapshot, evaluator_version: 'evaluate-v1',
    rubric_version: 'rubric-v1', prompt_version: 'prompt-v1', model: 'fixture', state: 'ready', reason: null, retrieval_complete: true, evaluations,
    score: { percent: 67.89, assessed_percent: 80, unknown_count: 1, requirement_count: 4, unknown_weight: 1, total_weight: 5,
      components: evaluations.map((item, index) => ({ requirement_id: item.requirement_id, status: item.status, weight: index === 3 ? 0 : 2, earned_units: index === 0 ? 4 : index === 1 ? 2 : 0, possible_units: index === 3 ? 0 : 4 })) } };
  return value;
}

test('shows server scores, coverage, distinct assessments and links to exact historical evidence', async () => {
  data = withMatch(); const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' });
  expect(screen.getByText('67.89%')).toBeVisible(); expect(screen.getByText('80% of weighted job requirements assessed')).toBeVisible();
  expect(screen.getByText(/does not guarantee eligibility/)).toBeVisible(); await user.click(screen.getByText('Requirement assessments (4)'));
  for (const name of ['Matched (1)', 'Partial (1)', 'Missing (1)', 'Unknown (1)']) expect(screen.getByRole('heading', { name })).toBeVisible();
  expect(screen.getByText('Score contribution: 2 of 4 points · Weight 2')).toBeVisible(); expect(screen.getByText(/checklist item; excluded/)).toBeVisible();
  const profile = within(screen.getByRole('region', { name: 'Matched (1)' })).getByRole('link', { name: 'Python' });
  await user.click(profile); const target = document.querySelector(profile.getAttribute('href') ?? 'missing'); expect(target).toHaveFocus(); expect(target).toBeVisible();
  expect(screen.getByRole('link', { name: 'Review your current profile' })).toHaveAttribute('href', '/profile');
  const source = within(screen.getByRole('region', { name: 'Matched (1)' })).getByRole('link', { name: 'Source excerpt 1' });
  await user.click(source); expect(document.querySelector(source.getAttribute('href') ?? 'missing')).toHaveFocus();
  expect(within(document.querySelector(source.getAttribute('href') ?? 'missing') as HTMLElement).getByText('Python required')).toBeVisible();
});

test.each([false, true])('labels preserved scores outdated while replacement is pending, profile changed=%s', async profile_changed => {
  data = withMatch(); if (!data.match_context) throw new Error('Missing context'); data.match_context.profile_changed = profile_changed;
  data.status = { ...data.status, match_current: false, match_stale: true, match_pending: true, match_state: 'stale' };
  mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); expect(screen.getByText('Outdated result')).toBeVisible();
  expect(screen.getByText('Outdated match score')).toBeVisible(); expect(screen.getByText('67.89%')).toBeVisible();
  expect(screen.getByText(profile_changed ? /accepted profile changed/ : /newer evaluation is needed/)).toBeVisible(); expect(button('Reevaluate profile match')).toBeDisabled();
});

test('keeps unknown scores and empty assessments distinct from zero or no requirements', async () => {
  data = withMatch(); if (!data.match) throw new Error('Missing match');
  data.match = { ...data.match, state: 'unknown', evaluations: [], retrieval_complete: false, score: { ...data.match.score, percent: null, assessed_percent: null, components: [], requirement_count: 0, unknown_count: 0 } };
  data.match_context = null; mount(); await screen.findByRole('heading', { name: 'Senior engineer' });
  expect(screen.getByText('Score unknown')).toBeVisible(); expect(screen.getByText('Coverage unknown')).toBeVisible(); expect(screen.getByText(/No requirement assessments/)).toBeVisible();
  expect(screen.getByText(/Evidence retrieval was incomplete/)).toBeVisible(); expect(screen.getByText(/Historical profile and source evidence is unavailable/)).toBeVisible();
});

test('preserves a valid zero score and labels incomplete evidence without using current facts', async () => {
  data = withMatch(); if (!data.match || !data.match_context) throw new Error('Missing match'); data.match.score.percent = 0;
  data.match.score.components = data.match.score.components.map(value => ({ ...value, status: 'unknown' }));
  data.match_context.profile_facts = []; data.match_context.citations = {}; data.match_context.requirements.items = [];
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); expect(screen.getByText('0%')).toBeVisible();
  await user.click(screen.getByText('Requirement assessments (4)')); expect(screen.getAllByText('Source excerpt unavailable')).toHaveLength(4);
  expect(screen.getAllByText(/Accepted fact unavailable/)).toHaveLength(3); expect(screen.getAllByText(/Requirement text unavailable/)).toHaveLength(4);
  expect(screen.getAllByText(/Unknown evidence; excluded/)).toHaveLength(3);
});

test.each(['failed', 'unavailable'] as const)('withholds misleading scores from a %s artifact', async state => {
  data = withMatch(); if (!data.match) throw new Error('Missing match'); data.match.state = state;
  data.status = { ...data.status, match_state: 'failed', match_current: false, match_reason: 'quota' };
  mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); expect(screen.queryByText('67.89%')).not.toBeInTheDocument();
  expect(screen.getByText('Evaluation failed')).toBeVisible(); expect(screen.getByText(/Provider capacity is exhausted/)).toBeVisible(); expect(button('Reevaluate profile match')).toBeEnabled();
});

test('renders rationale, profile facts and linked source excerpts as literal text', async () => {
  data = withMatch(); if (!data.match || !data.match_context) throw new Error('Missing match'); const hostile = '<script>alert("private")</script>';
  data.match.evaluations.forEach(item => { item.rationale = hostile; }); data.match_context.profile_facts.forEach(item => { item.skill = hostile; item.description = hostile; });
  Object.values(data.match_context.citations).forEach(span => { span.quote = hostile; span.paragraph_id = owner.id; }); data.match_context.source.paragraphs = [];
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(screen.getByText('Requirement assessments (4)'));
  await user.click(screen.getByText('Profile facts used as evidence (3)')); await user.click(screen.getByText('Job source used as evidence (4)'));
  expect(screen.getAllByText(hostile).length).toBeGreaterThan(4); expect(screen.getAllByText(/Source paragraph unavailable/).length).toBeGreaterThan(0);
  expect(document.querySelector('script')).toBeNull();
});

test('submits one CSRF-protected reevaluation and immediately marks preserved results outdated', async () => {
  data = withMatch(); const deferred = gate(); let writes = 0, body: unknown;
  server.use(http.post(path + '/reevaluate', async ({ request }) => { writes++; body = await request.json(); expect(request.headers.get('X-CSRF-Token')).toBe(owner.csrf_token); await deferred.pending; return HttpResponse.json({ generation: 2, status: 'accepted' }, { status: 202 }); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' });
  await user.dblClick(button('Reevaluate profile match')); expect(button('Requesting reevaluation…')).toBeDisabled(); expect(writes).toBe(1);
  expect(Object.keys(body as object)).toEqual(['idempotency_key']); expect((body as { idempotency_key: string }).idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
  server.use(http.get(path, () => failure(503))); deferred.release(); await screen.findByText(/Reevaluation requested/);
  expect(screen.getByText('Outdated match score')).toBeVisible(); expect(screen.getByText('67.89%')).toBeVisible(); expect(button('Reevaluate profile match')).toBeDisabled();
});

test.each([0, 503])('retries an uncertain reevaluation request with the same idempotency key (%s)', async status => {
  data = withMatch(); const keys: unknown[] = []; let fail = true;
  server.use(http.post(path + '/reevaluate', async ({ request }) => { keys.push(await request.json()); return fail ? status ? failure(status) : HttpResponse.error() : HttpResponse.json({ generation: 2, status: 'accepted' }, { status: 202 }); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Reevaluate profile match'));
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be confirmed'); expect(screen.getByText('67.89%')).toBeVisible(); fail = false;
  data.status = { ...data.status, match_current: false, match_pending: true, match_stale: true, match_state: 'stale', reevaluations_remaining: 2 };
  await user.click(button('Retry reevaluation request')); await screen.findByText(/Reevaluation requested/); expect(keys).toHaveLength(2); expect(keys[1]).toEqual(keys[0]);
  expect(screen.getByText(/2 reevaluation requests remain/)).toBeVisible();
});

test.each([409, 429])('reports a refused reevaluation safely and refreshes allowance/context (%s)', async status => {
  data = withMatch(); server.use(http.post(path + '/reevaluate', () => { if (status === 429) data.status.reevaluations_remaining = 0; return failure(status); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Reevaluate profile match'));
  expect(await screen.findByRole('alert')).toHaveTextContent(status === 429 ? 'allowance exhausted' : 'evidence or access changed');
  if (status === 429) await waitFor(() => { expect(button('Retry reevaluation request')).toBeDisabled(); }); expect(screen.queryByText('Private failure detail')).not.toBeInTheDocument();
});

test.each([401, 403])('discards match evidence when reevaluation authorization expires (%s)', async status => {
  data = withMatch(); server.use(http.post(path + '/reevaluate', () => { current = null; return failure(status); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Reevaluate profile match'));
  await screen.findByText(/Your session has expired/); expect(screen.queryByText('67.89%')).not.toBeInTheDocument(); expect(api.queryClient.getQueriesData({ queryKey: ['job-detail'] })).toEqual([]);
});

test('never restores the prior account’s match when a pending reevaluation response arrives', async () => {
  data = withMatch(); const deferred = gate(); let aborted = false;
  server.use(http.post(path + '/reevaluate', async ({ request }) => { request.signal.addEventListener('abort', () => { aborted = true; }); await deferred.pending; return HttpResponse.json({ generation: 2, status: 'accepted' }, { status: 202 }); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Reevaluate profile match'));
  server.use(http.get(path, () => failure(404))); act(() => { current = { ...owner, id: snapshot }; api.acceptSession(current); }); await screen.findByText('Job unavailable');
  deferred.release(); await act(async () => { await deferred.pending; }); expect(aborted).toBe(true); expect(screen.queryByText('67.89%')).not.toBeInTheDocument();
  expect(api.queryClient.getQueriesData({ queryKey: ['job-detail', owner.id] })).toEqual([]);
});

test('refreshes the viewed alias after reevaluating its canonical job', async () => {
  data = withMatch(); data.status.id = snapshot; let reads = 0;
  server.use(http.get(path, () => { reads++; return HttpResponse.json(data); }), http.post(`http://localhost/api/v1/jobs/${snapshot}/reevaluate`, () => {
    data.status = { ...data.status, match_state: 'stale', match_stale: true, match_current: false, match_pending: true, reevaluations_remaining: 2 };
    return HttpResponse.json({ generation: 2, status: 'accepted' }, { status: 202 });
  }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Reevaluate profile match'));
  await screen.findByText(/2 reevaluation requests remain/); expect(reads).toBe(2); expect(screen.getByText('Outdated match score')).toBeVisible();
});

type Stage = components['schemas']['StageStatus'];
function stages(changes: Partial<Record<Stage['stage'], Stage['state']>> = {}): Stage[] {
  return (['fetch', 'text', 'clean', 'chunks', 'embed', 'metadata', 'requirements'] as const).map(stage => ({ stage, state: changes[stage] ?? 'completed',
    revision: `${stage}:private-task-version`, generation: 0, reason: null, created_at: now, updated_at: now, retryable: changes[stage] === 'failed' }));
}
const processing = () => within(screen.getByRole('region', { name: 'Processing status' }));
async function advance(ms = 50) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
function fakeClock() { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] }); }

test('polls active stages only while visible, refreshes content on completion, and stops terminal polling', async () => {
  fakeClock(); data.status.stages = stages({ embed: 'running' }); data.status.embeddings_ready = false; let reads = 0;
  server.use(http.get(path + '/status', () => { reads++; return HttpResponse.json(data.status); })); mount(); await advance();
  await vi.waitFor(() => { expect(screen.getByRole('heading', { name: 'Senior engineer' })).toBeVisible(); });
  await advance(4800); expect(reads).toBe(0); await advance(300); expect(reads).toBe(1);
  expect(processing().getByText('Processing')).toBeVisible(); expect(screen.queryByText('Salary unknown')).not.toBeInTheDocument();
  act(() => { focusManager.setFocused(false); }); await advance(15000); expect(reads).toBe(1);
  data.status.stages = stages(); data.status.embeddings_ready = true; if (!data.requirements) throw new Error('Missing requirements'); data.requirements.summary = 'Updated checklist';
  act(() => { focusManager.setFocused(true); }); await advance(5000); await advance();
  await vi.waitFor(() => { expect(screen.getByText('Updated checklist')).toBeVisible(); }); const completed = reads;
  expect(processing().getByText(/No active work is reported/)).toBeVisible(); await advance(20000); expect(reads).toBe(completed);
});

test('does not poll future waiting stages without an active attempt or a terminal unsupported source', async () => {
  fakeClock(); data.status = { ...data.status, analysis_ready: false, match_pending: true, match_current: false, stages: stages({ fetch: 'unsupported', text: 'pending', clean: 'pending' }).map(stage => ({ ...stage, generation: stage.stage === 'fetch' ? 0 : null })) };
  let reads = 0; server.use(http.get(path + '/status', () => { reads++; return HttpResponse.json(data.status); })); mount(); await advance();
  await vi.waitFor(() => { expect(screen.getByRole('heading', { name: 'Processing status' })).toBeVisible(); }); await advance(20000); expect(reads).toBe(0);
  expect(processing().getByText('Unsupported')).toBeVisible(); expect(processing().queryByRole('button', { name: /^Retry / })).not.toBeInTheDocument();
});

test('tracks a pending private evaluation and stops once it is current', async () => {
  fakeClock(); data = withMatch(); data.status = { ...data.status, stages: stages(), match_current: false, match_pending: true, match_stale: true, match_state: 'stale' }; let reads = 0;
  server.use(http.get(path + '/status', () => { reads++; return HttpResponse.json(data.status); })); mount(); await advance(); await vi.waitFor(() => { expect(screen.getByText('Outdated match score')).toBeVisible(); });
  data.status = { ...data.status, match_current: true, match_pending: false, match_stale: false, match_state: 'current' }; await advance(5000); await advance();
  await vi.waitFor(() => { expect(screen.getByText('Current evaluation')).toBeVisible(); }); const completed = reads; await advance(10000); expect(reads).toBe(completed);
});

test.each(['http_denied', 'quota', 'timed_out', 'invalid_response', 'limits', 'http_removed', 'empty_page', 'javascript_required', 'provider-secret-<script>'])('shows safe processing guidance for %s and keeps available content', async reason => {
  data.status.stages = stages({ fetch: 'failed', clean: 'degraded' }); data.status.stages[0] = { ...data.status.stages[0] as Stage, reason };
  mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); expect(processing().getByText('Partly ready')).toBeVisible();
  expect(processing().getByText(/Some evidence is incomplete/)).toBeVisible(); expect(screen.getByText('EUR 50000–70000 / year · gross')).toBeVisible();
  expect(processing().getByRole('button', { name: 'Retry posting download' })).toBeEnabled(); expect(screen.queryByText(/private-task-version/)).not.toBeInTheDocument();
  expect(screen.queryByText(/provider-secret/)).not.toBeInTheDocument(); expect(document.querySelector('script')).toBeNull();
  const message = reason === 'http_denied' ? /Source access is denied/ : reason === 'quota' ? /processing quota is exhausted/ : reason === 'timed_out' ? /Processing timed out/
    : reason === 'invalid_response' ? /evidence could not be verified/ : reason === 'limits' ? /Processing limits were reached/ : reason === 'http_removed' ? /original posting was removed/
      : reason === 'empty_page' ? /No usable job posting/ : reason === 'javascript_required' ? /source cannot be processed/ : /Processing could not complete/;
  expect(processing().getByText(message)).toBeVisible();
});

test('stops polling on a status read failure, keeps facts and resumes on explicit refresh', async () => {
  fakeClock(); data.status.stages = stages({ text: 'running' }); let reads = 0, fail = true;
  server.use(http.get(path + '/status', () => { reads++; return fail ? failure(503) : HttpResponse.json(data.status); })); mount(); await advance();
  await vi.waitFor(() => { expect(screen.getByRole('heading', { name: 'Senior engineer' })).toBeVisible(); }); await advance(5000); await advance();
  expect(processing().getByRole('alert')).toHaveTextContent('could not be refreshed'); const failed = reads; await advance(15000); expect(reads).toBe(failed);
  fail = false; act(() => { button('Refresh processing status').click(); }); await advance();
  await vi.waitFor(() => { expect(processing().queryByRole('alert')).not.toBeInTheDocument(); }); await advance(5000); expect(reads).toBeGreaterThan(failed + 1);
  expect(screen.getByRole('heading', { name: 'Senior engineer' })).toBeVisible();
});

test('cancels a pending status request and removes its cache on unmount', async () => {
  fakeClock(); data.status.stages = stages({ clean: 'running' }); const deferred = gate(); let started = false, aborted = false;
  server.use(http.get(path + '/status', async ({ request }) => { started = true; request.signal.addEventListener('abort', () => { aborted = true; }); await deferred.pending; return HttpResponse.json(data.status); }));
  const view = mount(); await advance(); await vi.waitFor(() => { expect(screen.getByRole('heading', { name: 'Senior engineer' })).toBeVisible(); }); await advance(5000);
  await vi.waitFor(() => { expect(started).toBe(true); }); view.unmount(); expect(aborted).toBe(true); deferred.release(); await advance();
  expect(api.queryClient.getQueriesData({ queryKey: ['job-status'] })).toEqual([]);
});

test('submits one permitted retry with the exact stage fence and displays the shared reopened attempt', async () => {
  data.status.stages = stages({ fetch: 'failed' }); const deferred = gate(); let writes = 0, body: unknown;
  server.use(http.post(path + '/retry', async ({ request }) => { writes++; body = await request.json(); expect(request.headers.get('X-CSRF-Token')).toBe(owner.csrf_token); await deferred.pending;
    data.status.stages = stages({ fetch: 'running' }).map(stage => ({ ...stage, generation: stage.stage === 'fetch' ? 1 : 0 })); data.status.retries_remaining = 4;
    return HttpResponse.json({ generation: 1, status: 'accepted' }, { status: 202 }); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.dblClick(button('Retry posting download'));
  expect(writes).toBe(1); expect(button('Requesting retry…')).toBeDisabled(); expect(body).toMatchObject({ stage: 'fetch', revision: 'fetch:private-task-version', expected_generation: 0 });
  expect(Object.keys(body as object).sort()).toEqual(['expected_generation', 'idempotency_key', 'revision', 'stage']); deferred.release();
  await screen.findByText(/4 processing retry requests remain/); expect(processing().getByText('Processing')).toBeVisible(); expect(screen.queryByRole('button', { name: 'Retry posting download' })).not.toBeInTheDocument();
});

test('reuses the retry receipt key after an uncertain response without hiding available content', async () => {
  data.status.stages = stages({ metadata: 'failed' }); const bodies: unknown[] = []; let fail = true;
  server.use(http.post(path + '/retry', async ({ request }) => { bodies.push(await request.json()); if (fail) return HttpResponse.error();
    data.status.stages = stages(); data.status.retries_remaining = 4; return HttpResponse.json({ generation: 1, status: 'accepted' }, { status: 202 }); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Retry job facts'));
  expect(await processing().findByRole('alert')).toHaveTextContent('could not be confirmed'); expect(screen.getByRole('heading', { name: 'Senior engineer' })).toBeVisible(); fail = false;
  await user.click(button('Retry job facts')); await screen.findByText(/Retry accepted/); expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
});

test.each([409, 429])('refreshes status after a retry refusal %s without displaying private errors', async status => {
  data.status.stages = stages({ fetch: 'failed' }); server.use(http.post(path + '/retry', () => { if (status === 429) data.status.retries_remaining = 0; else data.status.stages = stages({ fetch: 'running' }); return failure(status); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Retry posting download'));
  expect(await processing().findByRole('alert')).toHaveTextContent(status === 429 ? 'allowance exhausted' : 'attempt changed');
  if (status === 429) await waitFor(() => { expect(button('Retry posting download')).toBeDisabled(); });
  else await waitFor(() => { expect(screen.queryByRole('button', { name: 'Retry posting download' })).not.toBeInTheDocument(); });
  expect(screen.queryByText('Private failure detail')).not.toBeInTheDocument();
});

test.each([401, 403])('clears all private processing state when retry authorization expires (%s)', async status => {
  data.status.stages = stages({ fetch: 'failed' }); server.use(http.post(path + '/retry', () => { current = null; return failure(status); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Retry posting download')); await screen.findByText(/Your session has expired/);
  expect(api.queryClient.getQueriesData({ queryKey: ['job-status'] })).toEqual([]); expect(screen.queryByRole('heading', { name: 'Senior engineer' })).not.toBeInTheDocument();
});

test('never exposes prior-owner processing state after a late retry response', async () => {
  data.status.stages = stages({ fetch: 'failed' }); const deferred = gate(); let aborted = false;
  server.use(http.post(path + '/retry', async ({ request }) => { request.signal.addEventListener('abort', () => { aborted = true; }); await deferred.pending; return HttpResponse.json({ generation: 1, status: 'accepted' }, { status: 202 }); }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Retry posting download'));
  server.use(http.get(path, () => failure(404))); act(() => { current = { ...owner, id: snapshot }; api.acceptSession(current); }); await screen.findByText('Job unavailable');
  deferred.release(); await act(async () => { await deferred.pending; }); expect(aborted).toBe(true); expect(screen.queryByRole('heading', { name: 'Processing status' })).not.toBeInTheDocument();
  expect(api.queryClient.getQueriesData({ queryKey: ['job-status', owner.id] })).toEqual([]);
});

test('uses fresh shared status when a retry receipt refers to an already completed older attempt', async () => {
  data.status.stages = stages({ fetch: 'failed' }); server.use(http.post(path + '/retry', () => {
    data.status.stages = stages().map(stage => ({ ...stage, revision: `${stage.stage}:new-version`, generation: 2 }));
    return HttpResponse.json({ generation: 1, status: 'accepted' }, { status: 202 });
  }));
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' }); await user.click(button('Retry posting download'));
  await screen.findByText(/Retry accepted/); expect(processing().getByText(/No active work is reported/)).toBeVisible();
  expect(processing().queryByRole('button', { name: /^Retry / })).not.toBeInTheDocument(); expect(processing().queryByText('Pending')).not.toBeInTheDocument();
});

test('starts polling when reevaluation changes a previously terminal detail state', async () => {
  fakeClock(); data = withMatch(); data.status.stages = stages(); let reads = 0;
  server.use(http.get(path + '/status', () => { reads++; return HttpResponse.json(data.status); }), http.post(path + '/reevaluate', () => {
    data.status = { ...data.status, match_state: 'stale', match_current: false, match_pending: true, match_stale: true }; return HttpResponse.json({ generation: 2, status: 'accepted' }, { status: 202 });
  }));
  mount(); await advance(); await vi.waitFor(() => { expect(button('Reevaluate profile match')).toBeEnabled(); });
  act(() => { button('Reevaluate profile match').click(); }); await advance(); await vi.waitFor(() => { expect(screen.getByText('Outdated match score')).toBeVisible(); });
  expect(processing().getByText(/Active work refreshes/)).toBeVisible(); await advance(5000); expect(reads).toBeGreaterThan(0);
});

test('removes available detail content when a status refresh reports lost discovery access', async () => {
  data.status.stages = stages(); const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: 'Senior engineer' });
  server.use(http.get(path + '/status', () => failure(404)), http.get(path, () => failure(404))); await user.click(button('Refresh processing status'));
  await screen.findByText('Job unavailable'); expect(screen.queryByRole('heading', { name: 'Senior engineer' })).not.toBeInTheDocument();
  expect(api.queryClient.getQueriesData({ queryKey: ['job-status'] })).toEqual([]);
});
