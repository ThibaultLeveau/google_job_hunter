import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { components } from '../../../generated/api';
import App from '../../App';
import { api, sessionKey, type Session } from '../../shared/api';
import { server } from '../../test/setup';

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
afterEach(() => { api.clearSession(); api.queryClient.clear(); });

test('renders factual summaries with units, assessment coverage, safe links and timestamps', async () => {
  mount(); const link = await screen.findByRole('link', { name: 'Senior software engineer' });
  expect(link).toHaveAttribute('href', `/jobs/${items[0]?.id ?? ''}`);
  expect(screen.getByText('Example company')).toBeVisible();
  expect(screen.getByText('Paris')).toBeVisible(); expect(screen.getByText('Hybrid')).toBeVisible();
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
});


test('refreshes an empty list, reports a failed refresh and recovers to new discoveries', async () => {
  items = []; const user = userEvent.setup(); mount(); await screen.findByText('No discoveries yet');
  server.use(http.get(path, () => failure(503))); await user.click(button('Refresh jobs'));
  expect(await screen.findByText('Jobs could not be loaded')).toBeVisible();
  items = [job()]; server.use(http.get(path, () => HttpResponse.json({ items, next_cursor: null })));
  await user.click(button('Retry loading jobs')); expect(await screen.findByText('Senior software engineer')).toBeVisible();
});
