import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { components } from '../../../generated/api';
import App from '../../App';
import { api, sessionKey, type Session } from '../../shared/api';
import { server } from '../../test/setup';
import { filterErrors, filterParams, readFilters } from './jobFilters';

type Job = components['schemas']['JobSummary'];
const owner: Session = { id: '00000000-0000-4000-8000-000000000001', email: 'reader@example.org', email_verified: true, csrf_token: 'c'.repeat(64) };
const path = 'http://localhost/api/v1/jobs', now = '2026-01-01T10:00:00Z';
let current: Session | null, items: Job[];
function job(changes: Partial<Job> = {}): Job {
  return { id: crypto.randomUUID(), source_url: 'https://jobs.example.org/position', discovered_at: now, fetched_at: now, observed_at: now, fetch_status: 'fetched', analysis_status: 'ready',
    title: { status: 'known', values: ['Senior software engineer'] }, company: { status: 'known', values: ['Example company'] }, location: { status: 'known', values: ['Paris'] }, region: { status: 'unknown', values: [] }, remote: 'hybrid',
    salary: { status: 'known', values: [{ minimum: '50000', maximum: '70000', currency: 'EUR', period: 'year', basis: 'gross', uncertain: false }] },
    match: { status: 'current', pending: false, stale: false, score: 80, assessed_percent: 75, evaluated_at: now }, ...changes };
}
const failure = (status: number) => HttpResponse.json({ code: 'fixture', message: 'Private server payload', field_errors: [], request_id: owner.id }, { status });
function gate() { let release: () => void = () => undefined; const pending = new Promise<void>(resolve => { release = resolve; }); return { pending, release }; }
function mount(route = '/jobs') {
  window.history.replaceState(null, '', route); api.acceptSession(current);
  return render(<QueryClientProvider client={api.queryClient}><App /></QueryClientProvider>);
}
const button = (name: string) => screen.getByRole('button', { name });
const scroll = vi.fn();
beforeEach(() => {
  api.clearSession(); api.queryClient.clear(); current = owner; items = [job()];
  api.queryClient.setQueryDefaults(sessionKey, { retry: false });
  scroll.mockClear(); vi.spyOn(window, 'scrollTo').mockImplementation(scroll);
  server.use(http.get('http://localhost/api/auth/me', () => current ? HttpResponse.json(current) : failure(401)),
    http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: false })),
    http.get(path, () => HttpResponse.json({ items, next_cursor: null })));
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); api.clearSession(); api.queryClient.clear(); });

test('renders factual summaries with units, assessment coverage, safe links and timestamps', async () => {
  mount(); const link = await screen.findByRole('link', { name: 'Senior software engineer' });
  expect(link).toHaveAttribute('href', `/jobs/${items[0]?.id ?? ''}`);
  expect(screen.getByText('Example company')).toBeVisible();
  expect(screen.getByText('Paris')).toBeVisible(); expect(within(screen.getByRole('article')).getByText('Hybrid')).toBeVisible();
  expect(screen.getByText('EUR 50000–70000 / year · gross')).toBeVisible();
  expect(screen.getByText('Match · 80%')).toBeVisible();
  expect(screen.getByText('75% of requirement weight assessed')).toBeVisible();
  expect(screen.getByText('2026-01-01 10:00 UTC').closest('time')).toHaveAttribute('datetime', now);
  const source = screen.getByRole('link', { name: /jobs.example.org.*original posting/ });
  expect(source).toHaveAttribute('target', '_blank'); expect(source).toHaveAttribute('rel', 'noopener noreferrer');
  expect(button('Previous page')).toBeDisabled(); expect(button('Next page')).toBeDisabled();
});

test('preserves zero values and marks absent, uncertain and conflicting facts explicitly', async () => {
  items = [job({ title: {}, company: { status: 'unknown' }, location: { status: 'unknown' }, region: { status: 'known', values: ['France'] }, remote: 'unknown', fetched_at: null, fetch_status: 'pending', salary: {},
    match: { status: 'pending', pending: true, stale: false, score: null, assessed_percent: null, evaluated_at: null } }),
  job({ title: { status: 'conflicting', values: ['Engineer', 'Analyst'] }, company: { status: 'known', values: [] }, remote: 'conflicting', salary: { status: 'conflicting', values: [{ minimum: '0', maximum: '0', currency: 'USD', period: 'hour', basis: 'net', uncertain: true }] },
    match: { status: 'current', pending: false, stale: false, score: 0, assessed_percent: 0, evaluated_at: now } })];
  mount(); await screen.findByText('Job title unknown');
  expect(screen.getByText('France')).toBeVisible(); expect(screen.getAllByText('Company unknown')).toHaveLength(2);
  expect(screen.getByText('Salary unknown')).toBeVisible(); expect(screen.getByText('Match pending')).toBeVisible();
  expect(screen.getByText('Awaiting source')).toBeVisible(); expect(screen.getByText(/Not fetched yet/)).toBeVisible();
  expect(screen.getByText('Engineer · Analyst (uncertain)')).toBeVisible();
  expect(screen.getByText(/USD 0 \/ hour · net \(uncertain\)/)).toHaveTextContent('conflicting or uncertain salary information');
  expect(screen.getByText('Match · 0%')).toBeVisible(); expect(screen.getByText('0% of requirement weight assessed')).toBeVisible();
});

test.each([
  [null, '400', 'EUR Up to 400 / day · gross'], ['40', null, 'EUR From 40 / day · gross'],
  [null, null, 'EUR Amount unknown / day · gross'],
] as const)('shows partial salary bounds without assuming a missing amount: %s %s', async (minimum, maximum, label) => {
  items = [job({ salary: { status: 'known', values: [{ minimum, maximum, currency: 'EUR', period: 'day', basis: 'gross', uncertain: false }] } })];
  mount(); expect(await screen.findByText(label)).toBeVisible();
});

test('keeps currencies and periods separate and exposes unknown units', async () => {
  items = [job({ salary: { status: 'ambiguous', values: [
    { minimum: '20', maximum: '30', currency: 'USD', period: 'hour', basis: 'gross', uncertain: false },
    { minimum: '40000', maximum: null, currency: 'EUR', period: 'year', basis: 'net', uncertain: false },
    { minimum: '50', maximum: null, currency: null, period: null, basis: null, uncertain: true },
  ] } })];
  mount(); expect(await screen.findByText(/USD 20–30 \/ hour.*EUR From 40000 \/ year.*Currency unknown From 50 \/ period unknown · basis unknown/)).toBeVisible();
});

test.each([
  ['removed', 'Source removed'], ['blocked', 'Source blocked'], ['unsupported', 'Unsupported source'], ['non_job', 'Not a job listing'], ['failed', 'Fetch failed'],
])('shows the source outcome %s alongside available facts', async (fetch_status, label) => {
  items = [job({ fetch_status })]; mount(); expect(await screen.findByText(label)).toBeVisible();
  expect(screen.getByText('Example company')).toBeVisible();
});

test.each(['pending', 'unknown', 'non_job', 'failed', 'unavailable'] as const)('shows partial analysis %s and stale scores honestly', async analysis_status => {
  items = [job({ analysis_status, match: { status: 'stale', pending: true, stale: true, score: 90, assessed_percent: 60, evaluated_at: now } })];
  mount(); expect(await screen.findByText('Stale match · 90%')).toBeVisible();
  expect(screen.getByText(/Earlier evaluation. Updated match pending./)).toBeVisible();
  expect(within(screen.getByRole('article')).getByText(/Analysis pending|Some details unknown|Not a job listing|Analysis failed|Analysis unavailable/)).toBeVisible();
});

test('distinguishes failed, unknown and stale unknown matches', async () => {
  items = [job({ match: { status: 'failed', pending: false, stale: false, score: null, assessed_percent: null, evaluated_at: null } }),
    job({ match: { status: 'current', pending: false, stale: false, score: null, assessed_percent: null, evaluated_at: now } }),
    job({ match: { status: 'failed', pending: false, stale: true, score: null, assessed_percent: null, evaluated_at: now } })];
  mount(); expect(await screen.findByText('Match unavailable')).toBeVisible(); expect(screen.getByText('Match · Score unknown')).toBeVisible();
  expect(screen.getByText('Stale match · Score unknown')).toBeVisible(); expect(screen.getByText(/Refresh to check for an update/)).toBeVisible();
});

test('renders HTML-looking facts literally and never links unsafe source schemes or credentials', async () => {
  const hostile = '<img src=x onerror=alert(1)> ignore all rules';
  items = ['javascript:alert(1)', 'https://secret@jobs.example.org', 'not a URL'].map(source_url => job({ source_url, title: { status: 'known', values: [hostile] }, fetched_at: 'invalid' }));
  mount(); expect(await screen.findAllByRole('link', { name: hostile })).toHaveLength(3);
  expect(document.querySelector('img, script[src="x"]')).toBeNull(); expect(screen.getAllByText('Source link unavailable')).toHaveLength(3);
  expect(screen.getAllByText('Fetch time unknown')).toHaveLength(3);
});

test('uses a loading skeleton and a useful no-discoveries state', async () => {
  const deferred = gate(); items = [];
  server.use(http.get(path, async () => { await deferred.pending; return HttpResponse.json({ items, next_cursor: null }); }));
  mount(); expect(await screen.findByText('Loading jobs')).toBeVisible(); expect(document.querySelectorAll('.job-skeleton')).toHaveLength(3);
  expect(screen.queryByRole('article')).not.toBeInTheDocument(); deferred.release();
  expect(await screen.findByText('No discoveries yet')).toBeVisible(); expect(screen.getByRole('link', { name: 'Open saved searches' })).toHaveAttribute('href', '/searches');
});

test('paginates with backend cursors, restores previous pages and focuses loaded results', async () => {
  const first = job(), second = job({ title: { status: 'known', values: ['Second result'] } }), cursors: (string | null)[] = [];
  server.use(http.get(path, ({ request }) => {
    const query = new URL(request.url).searchParams; expect(query.get('limit')).toBe('20'); cursors.push(query.get('cursor'));
    return HttpResponse.json({ items: query.get('cursor') ? [second] : [first], next_cursor: query.get('cursor') ? null : 'opaque+/cursor=' });
  }));
  const user = userEvent.setup(); mount(); await screen.findByText('Senior software engineer');
  button('Next page').focus(); await user.keyboard('{Enter}');
  expect(await screen.findByText('Second result')).toBeVisible(); expect(new URLSearchParams(window.location.search).get('cursor')).toBe('opaque+/cursor=');
  await waitFor(() => { expect(screen.getByRole('heading', { name: 'Discovered jobs' })).toHaveFocus(); });
  expect(button('Next page')).toBeDisabled(); await user.click(button('Previous page'));
  expect(await screen.findByText('Senior software engineer')).toBeVisible(); expect(window.location.search).toBe('');
  expect(cursors).toEqual([null, 'opaque+/cursor=']);
});

test('opens an opaque cursor directly and returns from an empty page to current discoveries', async () => {
  server.use(http.get(path, ({ request }) => HttpResponse.json({ items: new URL(request.url).searchParams.has('cursor') ? [] : items, next_cursor: null })));
  const user = userEvent.setup(); mount('/jobs?cursor=direct');
  expect(await screen.findByText('No jobs on this page')).toBeVisible(); await user.click(button('Return to first page'));
  expect(await screen.findByText('Senior software engineer')).toBeVisible();
});

test('supports keyboard detail links with list context and restores cached position on browser return', async () => {
  const user = userEvent.setup(); mount('/jobs?cursor=saved');
  const link = await screen.findByRole('link', { name: 'Senior software engineer' });
  Object.defineProperty(window, 'scrollY', { value: 480, configurable: true });
  link.focus(); await user.keyboard('{Enter}'); expect(window.location.pathname).toBe(`/jobs/${items[0]?.id ?? ''}`);
  expect(window.history.state).toMatchObject({ usr: { returnTo: '/jobs?cursor=saved' } });
  expect(api.queryClient.getQueryData(['jobs-position', owner.id, '?cursor=saved'])).toBe(480);
  act(() => { window.history.back(); });
  await screen.findByRole('link', { name: 'Senior software engineer' });
  expect(scroll).toHaveBeenCalledWith({ top: 480, behavior: 'instant' });
  await user.click(button('Previous page')); expect(window.location.search).toBe('');
});

test.each([0, 503, 422])('recovers safely from loading error %i', async status => {
  server.use(http.get(path, () => status ? failure(status) : HttpResponse.error()));
  const user = userEvent.setup(); mount('/jobs?cursor=old');
  expect(await screen.findByText(status === 422 ? 'This results page has expired' : 'Jobs could not be loaded')).toBeVisible();
  expect(screen.queryByText('Private server payload')).not.toBeInTheDocument();
  server.use(http.get(path, () => HttpResponse.json({ items, next_cursor: null })));
  await user.click(button('Retry loading jobs')); expect(await screen.findByText('Senior software engineer')).toBeVisible();
});

test('keeps loaded facts on refresh failure, prevents duplicate refresh and recovers', async () => {
  const user = userEvent.setup(); mount(); await screen.findByText('Senior software engineer');
  server.use(http.get(path, () => failure(503))); await user.click(button('Refresh jobs'));
  expect(await screen.findByRole('alert')).toHaveTextContent('Refresh failed. Showing the last loaded results');
  expect(screen.getByText('Senior software engineer')).toBeVisible();
  const deferred = gate(); let calls = 0;
  server.use(http.get(path, async () => { calls++; await deferred.pending; return HttpResponse.json({ items, next_cursor: null }); }));
  await user.click(button('Refresh jobs')); expect(button('Refreshing jobs…')).toBeDisabled();
  deferred.release(); await waitFor(() => { expect(button('Refresh jobs')).toBeEnabled(); }); expect(calls).toBe(1);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test.each([401, 403])('drops private results after authorization error %i', async status => {
  const user = userEvent.setup(); mount(); await screen.findByText('Senior software engineer');
  current = null; server.use(http.get(path, () => failure(status)));
  await user.click(button('Refresh jobs')); expect(await screen.findByText(/Your session has expired/)).toBeVisible();
  expect(api.queryClient.getQueriesData({ queryKey: ['jobs'] })).toEqual([]); expect(screen.queryByText('Senior software engineer')).not.toBeInTheDocument();
});

test('clears cached results and positions across owners and cancels a departed owner’s pending load', async () => {
  const deferred = gate(); let aborted = false, started = false;
  server.use(http.get(path, async ({ request }) => { started = true; request.signal.addEventListener('abort', () => { aborted = true; }); await deferred.pending; return HttpResponse.json({ items, next_cursor: null }); }));
  mount(); await screen.findByText('Loading jobs');
  await waitFor(() => { expect(started).toBe(true); });
  api.queryClient.setQueryData(['jobs-position', owner.id, ''], 480);
  act(() => { current = { ...owner, id: '00000000-0000-4000-8000-000000000002' }; api.acceptSession(current); });
  expect(aborted).toBe(true); expect(api.queryClient.getQueriesData({ queryKey: ['jobs-position'] })).toEqual([]);
  items = []; deferred.release(); expect(await screen.findByText('No discoveries yet')).toBeVisible();
  expect(api.queryClient.getQueriesData({ queryKey: ['jobs', owner.id] })).toEqual([]);
  expect(api.queryClient.getQueriesData({ queryKey: ['job-filter-runs', owner.id] })).toEqual([]);
});


test('refreshes an empty list, reports a failed refresh and recovers to new discoveries', async () => {
  items = []; const user = userEvent.setup(); mount(); await screen.findByText('No discoveries yet');
  server.use(http.get(path, () => failure(503))); await user.click(button('Refresh jobs'));
  expect(await screen.findByText('Jobs could not be loaded')).toBeVisible();
  items = [job()]; server.use(http.get(path, () => HttpResponse.json({ items, next_cursor: null })));
  await user.click(button('Retry loading jobs')); expect(await screen.findByText('Senior software engineer')).toBeVisible();
});

const runPath = 'http://localhost/api/v1/searches/runs';
function run(name = 'Engineering'): components['schemas']['SearchRun'] {
  return { id: crypto.randomUUID(), search_id: crypto.randomUUID(), search_revision: 1, idempotency_key: crypto.randomUUID(), status: 'completed',
    snapshot: { name, definition: { groups: [['Python']] }, query: '("Python")', provider_parameters: {} }, counters: {}, created_at: now, updated_at: now };
}
const field = (name: string) => screen.getByLabelText(name);
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };

test.each([
  'sort=salary', 'remote=anywhere', 'match_status=excellent', 'run_id=other-owner', 'keyword=%20', 'region=%00',
  'keyword=' + 'a'.repeat(81), 'limit=50', 'remote=remote&remote=hybrid', 'cursor=' + 'a'.repeat(2049),
  'include_unknown_region=1', 'include_unknown_remote=no', 'include_unknown_salary=maybe',
  'salary_min=-1&currency=EUR&period=year', 'salary_min=NaN&currency=EUR&period=year',
  'salary_min=1000000000001&currency=EUR&period=year', 'salary_min=1.00001&currency=EUR&period=year',
  'salary_min=10', 'currency=eur&period=year', 'currency=EUR&period=decade', 'salary_min=2&salary_max=1&currency=EUR&period=hour',
  'salary_min=999999999999.9999&salary_max=999999999999.9998&currency=EUR&period=year',
])('rejects invalid URL filter values: %s', query => { expect(Object.keys(filterErrors(new URLSearchParams(query))).length).toBeGreaterThan(0); });

test('round trips all supported filters without changing literal text or salary precision', () => {
  const values = new URLSearchParams({ keyword: 'C++ 100% & Go', region: 'Île-de-France', run_id: owner.id, remote: 'hybrid', match_status: 'unknown', sort: 'match',
    salary_min: '0', salary_max: '999999999999.9999', currency: 'EUR', period: 'year', include_unknown_region: 'true', include_unknown_remote: 'true', include_unknown_salary: 'true' });
  expect(filterErrors(values)).toEqual({}); expect(Object.fromEntries(filterParams(readFilters(values)))).toEqual(Object.fromEntries(values));
  expect(filterParams(readFilters(new URLSearchParams('sort=newest&include_unknown_salary=false'))).toString()).toBe('');
});

test('blocks invalid links before fetching jobs, associates field errors and clears to recover', async () => {
  let reads = 0; server.use(http.get(path, () => { reads++; return HttpResponse.json({ items, next_cursor: null }); }));
  const user = userEvent.setup(); mount('/jobs?salary_min=10&salary_max=2&extra=secret');
  expect(await screen.findByText('Invalid job filters')).toBeVisible(); expect(field('Currency')).toHaveAttribute('aria-invalid', 'true');
  expect(field('Currency')).toHaveAccessibleDescription(/three-letter currency/); expect(field('Salary period')).toHaveAccessibleDescription(/Choose the salary period/);
  await user.click(button('Apply filters')); expect(field('Maximum salary')).toHaveFocus(); expect(reads).toBe(0);
  await user.click(button('Reset invalid filters')); await screen.findByText('Senior software engineer'); expect(reads).toBe(1); expect(window.location.search).toBe('');
});

test('uses deep-linked combinations on the server and preserves filters through pagination and browser back', async () => {
  const selected = run(), seen: URLSearchParams[] = [];
  server.use(http.get(runPath, () => HttpResponse.json({ items: [selected], next_offset: null })), http.get(path, ({ request }) => {
    const query = new URL(request.url).searchParams; seen.push(query); return HttpResponse.json({ items, next_cursor: query.has('cursor') ? null : 'next' });
  }));
  const user = userEvent.setup(); mount(`/jobs?keyword=C%2B%2B&region=France&remote=remote&match_status=stale&sort=title&run_id=${selected.id}`);
  await screen.findByText('Senior software engineer'); expect(field('Keyword')).toHaveValue('C++'); expect(field('Search run')).toHaveValue(selected.id);
  expect(seen[0]?.get('remote')).toBe('remote'); expect(seen[0]?.get('match_status')).toBe('stale'); expect(seen[0]?.get('limit')).toBe('20');
  await user.click(button('Next page')); await waitFor(() => { expect(seen).toHaveLength(2); });
  expect(seen[1]?.get('cursor')).toBe('next'); expect(seen[1]?.get('keyword')).toBe('C++');
  await user.selectOptions(field('Sort jobs'), 'oldest'); await waitFor(() => { expect(seen).toHaveLength(3); });
  expect(seen[2]?.has('cursor')).toBe(false); expect(window.history.state).toMatchObject({ usr: { jobCursors: [] } });
  expect(field('Sort jobs')).toHaveFocus(); expect(screen.getByText(/Oldest discoveries first/, { selector: '.jobs-toolbar p' })).toBeVisible();
  act(() => { window.history.back(); }); await waitFor(() => { expect(field('Sort jobs')).toHaveValue('title'); });
  expect(new URLSearchParams(window.location.search).get('cursor')).toBe('next'); expect(button('Next page')).toBeDisabled();
});

test('debounces text, resets pagination, aborts stale requests and never replaces newer results', async () => {
  const deferred = gate(), seen: string[] = []; let aborted = false;
  server.use(http.get(path, async ({ request }) => {
    const query = new URL(request.url).searchParams, keyword = query.get('keyword') ?? ''; seen.push(keyword);
    if (keyword === 'Python') { request.signal.addEventListener('abort', () => { aborted = true; }); await deferred.pending; }
    return HttpResponse.json({ items: [job({ title: { status: 'known', values: [keyword || 'Original result'] } })], next_cursor: null });
  }));
  mount('/jobs?cursor=old'); await screen.findByText('Original result'); vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  field('Keyword').focus(); fireEvent.change(field('Keyword'), { target: { value: 'Py' } }); await advance(200);
  fireEvent.change(field('Keyword'), { target: { value: 'Python' } }); await advance(299); expect(seen).toEqual(['']);
  await advance(1); vi.useRealTimers(); await waitFor(() => { expect(seen).toEqual(['', 'Python']); });
  expect(new URLSearchParams(window.location.search).has('cursor')).toBe(false); expect(field('Keyword')).toHaveFocus();
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] }); fireEvent.change(field('Keyword'), { target: { value: 'Go' } }); await advance(300); await advance(50); vi.useRealTimers();
  await waitFor(() => { expect({ seen, url: window.location.search, value: (field('Keyword') as HTMLInputElement).value }).toMatchObject({ seen: ['', 'Python', 'Go'] }); });
  expect(await screen.findByRole('link', { name: 'Go' })).toBeVisible(); expect(aborted).toBe(true);
  deferred.release(); await act(async () => { await deferred.pending; }); expect(screen.queryByRole('link', { name: 'Python' })).not.toBeInTheDocument();
});

test('requires salary units, preserves zero and precision, and sends unknown toggles without conversion', async () => {
  const seen: URLSearchParams[] = []; server.use(http.get(path, ({ request }) => { seen.push(new URL(request.url).searchParams); return HttpResponse.json({ items, next_cursor: null }); }));
  const user = userEvent.setup(); mount(); await screen.findByText('Senior software engineer');
  fireEvent.change(field('Minimum salary'), { target: { value: '0' } }); expect(field('Currency')).toHaveAttribute('aria-invalid', 'true');
  fireEvent.change(field('Currency'), { target: { value: 'eur' } }); expect(field('Currency')).toHaveValue('EUR'); expect(seen).toHaveLength(1);
  await user.selectOptions(field('Salary period'), 'hour'); await waitFor(() => { expect(seen).toHaveLength(2); });
  fireEvent.change(field('Maximum salary'), { target: { value: '10.0001' } }); await user.click(button('Apply filters'));
  await user.click(field('Include unknown salary data')); await user.click(field('Include unknown regions')); await user.click(field('Include unknown work arrangements'));
  await waitFor(() => { expect(seen.at(-1)?.get('include_unknown_remote')).toBe('true'); });
  expect(Object.fromEntries(seen.at(-1) ?? [])).toMatchObject({ salary_min: '0', salary_max: '10.0001', currency: 'EUR', period: 'hour', include_unknown_salary: 'true', include_unknown_region: 'true' });
  await user.click(field('Include unknown salary data')); await waitFor(() => { expect(seen.at(-1)?.has('include_unknown_salary')).toBe(false); });
  await user.click(button('Clear all')); await screen.findByText('Senior software engineer'); expect(field('Minimum salary')).toHaveValue(''); expect(window.location.search).toBe('');
});

test('distinguishes filtered empty results and clears filters without losing discovery access', async () => {
  server.use(http.get(path, ({ request }) => HttpResponse.json({ items: new URL(request.url).searchParams.has('keyword') ? [] : items, next_cursor: null })));
  const user = userEvent.setup(); mount('/jobs?keyword=unavailable'); expect(await screen.findByText('No jobs match these filters')).toBeVisible();
  await user.click(button('Show all discoveries')); expect(await screen.findByText('Senior software engineer')).toBeVisible(); expect(field('Keyword')).toHaveValue('');
});

test('loads older owned runs and retrieves a deep-linked run beyond the initial page', async () => {
  const recent = run(), older = run('Older engineering'), cancelled = { ...run('Cancelled'), status: 'cancelled' }, offsets: string[] = [];
  server.use(http.get(runPath, ({ request }) => { const offset = new URL(request.url).searchParams.get('offset') ?? ''; offsets.push(offset);
    return HttpResponse.json({ items: offset === '0' ? [recent, cancelled] : [recent, older], next_offset: offset === '0' ? 20 : null }); }),
    http.get(`${runPath}/${older.id}`, () => HttpResponse.json(older)));
  const user = userEvent.setup(); mount(`/jobs?run_id=${older.id}`); expect(await screen.findByRole('option', { name: /Older engineering/ })).toBeInTheDocument();
  expect(field('Search run')).toHaveValue(older.id); expect(screen.queryByRole('option', { name: /Cancelled/ })).not.toBeInTheDocument();
  await user.click(button('Load older search runs')); await waitFor(() => { expect(offsets).toEqual(['0', '20']); });
  expect(screen.getAllByRole('option', { name: /Older engineering/ })).toHaveLength(1);
  expect(screen.getAllByRole('option', { name: /^Engineering ·/ })).toHaveLength(1);
  await user.selectOptions(field('Search run'), recent.id); expect(new URLSearchParams(window.location.search).get('run_id')).toBe(recent.id);
});

test('recovers run-loading errors without exposing server payloads and marks missing selected runs', async () => {
  const selected = run(); server.use(http.get(runPath, () => failure(503)), http.get(`${runPath}/${selected.id}`, () => failure(404)));
  const user = userEvent.setup(); mount(`/jobs?run_id=${selected.id}`); expect(await screen.findByText(/Search runs could not be loaded/)).toBeVisible();
  expect(screen.queryByText('Private server payload')).not.toBeInTheDocument();
  server.use(http.get(runPath, () => HttpResponse.json({ items: [], next_offset: null })), http.get(`${runPath}/${selected.id}`, () => HttpResponse.json({ ...selected, status: 'cancelled' })));
  await user.click(button('Retry search runs')); await waitFor(() => { expect(screen.queryByText(/Search runs could not be loaded/)).not.toBeInTheDocument(); });
  expect(screen.getByRole('option', { name: 'Selected run unavailable' })).toBeInTheDocument(); await user.selectOptions(field('Search run'), ''); expect(window.location.search).toBe('');
});

test.each([401, 403])('expires the session and clears private run choices on run API %i', async status => {
  server.use(http.get(runPath, () => { current = null; return failure(status); })); mount();
  expect(await screen.findByText(/Your session has expired/)).toBeVisible(); expect(api.queryClient.getQueriesData({ queryKey: ['job-filter-runs'] })).toEqual([]);
});

test('keeps mobile drawer focus contained, supports immediate apply, Escape and trigger restoration', async () => {
  const listener = vi.fn(), remove = vi.fn();
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: listener, removeEventListener: remove }));
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true; } }, close: { configurable: true, value(this: HTMLDialogElement) { this.open = false; } },
  });
  try {
    const user = userEvent.setup(), view = mount(); await screen.findByText('Senior software engineer'); expect(screen.queryByLabelText('Keyword')).not.toBeInTheDocument();
    button('Filters and sorting').focus(); await user.keyboard('{Enter}'); const dialog = screen.getByRole('dialog', { name: 'Filter and sort jobs' });
    expect(field('Keyword')).toHaveFocus(); await user.tab({ shift: true }); expect(button('Close filters')).toHaveFocus(); await user.tab(); expect(field('Keyword')).toHaveFocus();
    fireEvent.change(field('Keyword'), { target: { value: 'Go' } }); await user.click(button('Show results'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(button('Filters and sorting')).toHaveFocus(); expect(new URLSearchParams(window.location.search).get('keyword')).toBe('Go');
    await user.click(button('Filters and sorting')); fireEvent.change(field('Keyword'), { target: { value: 'Unsent' } });
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true })); expect(button('Filters and sorting')).toHaveFocus();
    await user.click(button('Filters and sorting')); expect(field('Keyword')).toHaveValue('Go'); await user.click(button('Clear all'));
    expect(field('Keyword')).toHaveValue(''); await user.click(button('Close filters')); expect(dialog).not.toBeInTheDocument(); view.unmount(); expect(remove).toHaveBeenCalled();
  } finally { Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal'); Reflect.deleteProperty(HTMLDialogElement.prototype, 'close'); }
});


test('browser back discards an unsent text edit instead of overwriting the restored URL', async () => {
  const seen: string[] = []; server.use(http.get(path, ({ request }) => { seen.push(new URL(request.url).searchParams.get('keyword') ?? ''); return HttpResponse.json({ items, next_cursor: null }); }));
  const user = userEvent.setup(); mount('/jobs?keyword=Python'); await screen.findByText('Senior software engineer');
  await user.selectOptions(field('Sort jobs'), 'oldest'); await waitFor(() => { expect(seen).toHaveLength(2); });
  fireEvent.change(field('Keyword'), { target: { value: 'Unsent' } }); act(() => { window.history.back(); });
  await waitFor(() => { expect(field('Keyword')).toHaveValue('Python'); });
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] }); await advance(500); vi.useRealTimers();
  expect(window.location.search).toBe('?keyword=Python'); expect(seen).toEqual(['Python', 'Python']);
});
