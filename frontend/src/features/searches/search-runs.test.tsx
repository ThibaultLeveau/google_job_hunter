import { focusManager, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { components } from '../../../generated/api';
import App from '../../App';
import { api, sessionKey, type Session } from '../../shared/api';
import { server } from '../../test/setup';

type Run = components['schemas']['SearchRun'];
type Saved = components['schemas']['SavedSearch'];
const owner: Session = { id: '00000000-0000-4000-8000-000000000001', email: 'owner@example.org', email_verified: true, csrf_token: 'c'.repeat(64) };
const path = 'http://localhost/api/v1/searches', date = '2026-01-01T00:00:00Z';
let current: Session | null, saved: Saved, runs: Run[], capability: components['schemas']['SearchCapability'], reads: number;
const failure = (status: number, retry = '') => HttpResponse.json({ code: 'fixture', message: 'Private provider detail', field_errors: [], request_id: owner.id }, { status, headers: { 'Retry-After': retry } });
function run(status: Run['status'] = 'pending', extra: Partial<Run> = {}): Run {
  return { id: crypto.randomUUID(), search_id: saved.id, search_revision: saved.revision, idempotency_key: crypto.randomUUID(), status,
    snapshot: { name: saved.name, definition: saved.definition, query: '("Python")', provider_parameters: {} }, counters: {}, created_at: date, updated_at: date, ...extra };
}
function gate() { let release: () => void = () => undefined; const pending = new Promise<void>(resolve => { release = resolve; }); return { pending, release }; }
const button = (name: string) => screen.getByRole('button', { name });
function mount() { window.history.replaceState(null, '', '/searches'); api.acceptSession(current); return render(<QueryClientProvider client={api.queryClient}><App /></QueryClientProvider>); }
async function choose() {
  const user = userEvent.setup(); mount(); await screen.findByRole('option', { name: 'Engineering · revision 1' }); await user.selectOptions(screen.getByLabelText('Saved search on this page'), saved.id);
  await waitFor(() => { expect(button('Run search')).toBeEnabled(); }); return user;
}
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
beforeEach(() => {
  api.clearSession(); api.queryClient.clear(); current = owner; reads = 0; runs = [];
  saved = { id: crypto.randomUUID(), name: 'Engineering', definition: { groups: [['Python']] }, revision: 1, created_at: date, updated_at: date };
  capability = { provider: 'fixture', status: 'fixture', max_pages: 2, max_results: 20 };
  for (const key of [sessionKey, ['searches'], ['search-runs'], ['search-provider']]) api.queryClient.setQueryDefaults(key, { retry: false });
  focusManager.setFocused(true);
  server.use(http.get('http://localhost/api/auth/me', () => current ? HttpResponse.json(current) : failure(401)),
    http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: false })),
    http.get(path, () => HttpResponse.json({ items: current?.id === owner.id ? [saved] : [], next_offset: null })),
    http.get(path + '/provider', () => HttpResponse.json(capability)),
    http.get(path + '/runs', () => { reads++; return HttpResponse.json({ items: runs, next_offset: null }); }));
});
afterEach(() => { vi.useRealTimers(); focusManager.setFocused(undefined); api.clearSession(); api.queryClient.clear(); });

test.each(['disabled', 'unconfigured'] as const)('explains %s setup and empty history without allowing execution', async status => {
  capability = { ...capability, provider: status === 'disabled' ? 'disabled' : 'serpapi', status };
  const user = userEvent.setup(); mount(); await screen.findByRole('option', { name: 'Engineering · revision 1' }); await user.selectOptions(screen.getByLabelText('Saved search on this page'), saved.id);
  expect(await screen.findByText(/Search configuration is required/)).toBeVisible(); expect(button('Run search')).toBeDisabled(); expect(await screen.findByText('No discovery runs yet.')).toBeVisible();
  expect(screen.getByText(/Discovery completion does not mean job analysis is complete/)).toBeVisible();
});

test('blocks double clicks, sends CSRF/revision/key and preserves confirmed history across reload', async () => {
  const deferred = gate(); let calls = 0;
  server.use(http.post(path + '/:id/runs', async ({ request, params }) => {
    calls++; expect(params.id).toBe(saved.id); expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    const body = await request.json() as components['schemas']['RunStart']; expect(body.expected_revision).toBe(1); expect(Object.keys(body).sort()).toEqual(['expected_revision', 'idempotency_key']);
    expect(body.idempotency_key).toMatch(/^[\da-f-]{36}$/u); runs = [run('running', { idempotency_key: body.idempotency_key })]; await deferred.pending; return HttpResponse.json(runs[0], { status: 202 });
  }));
  const user = await choose(); const start = button('Run search'); fireEvent.click(start); fireEvent.click(start);
  expect(await screen.findByRole('button', { name: 'Starting discovery…' })).toBeDisabled(); expect(screen.getByLabelText('Saved search on this page')).toBeDisabled();
  deferred.release(); expect(await screen.findByText(/Run request confirmed/)).toBeVisible(); expect(await screen.findByText('Discovering')).toBeVisible(); expect(calls).toBe(1);
  await user.click(button('Refresh run history')); expect(screen.getByText('Discovering')).toBeVisible();
  await user.click(screen.getByRole('link', { name: 'Jobs' })); await user.click(screen.getByRole('link', { name: 'Searches' }));
  expect(await screen.findByText('Discovering')).toBeVisible(); expect(calls).toBe(1);
});

test('retries an uncertain start with its original idempotency key and revision after a saved edit', async () => {
  let calls = 0, original: unknown;
  server.use(http.post(path + '/:id/runs', async ({ request }) => {
    calls++; const body = await request.json();
    if (calls === 1) { original = body; runs = [run()]; return HttpResponse.error(); }
    expect(body).toEqual(original); return HttpResponse.json(runs[0], { status: 202 });
  }));
  const user = await choose(); await user.click(button('Run search')); expect(await screen.findByText(/run request could not be confirmed/)).toBeVisible();
  saved = { ...saved, name: 'Changed elsewhere', revision: 2 };
  await act(async () => { await api.queryClient.invalidateQueries({ queryKey: ['searches'] }); });
  expect(screen.getByText('Requested search: Engineering · revision 1')).toBeVisible(); expect(calls).toBe(1);
  await user.click(button('Retry same run request')); expect(await screen.findByText(/Run request confirmed/)).toBeVisible(); expect(calls).toBe(2); expect(runs).toHaveLength(1);
});

test('reloads a confirmed stale revision before starting a fresh request', async () => {
  let calls = 0; const keys: string[] = [];
  server.use(http.post(path + '/:id/runs', async ({ request }) => {
    calls++; const body = await request.json() as components['schemas']['RunStart']; keys.push(body.idempotency_key);
    if (calls === 1) { saved = { ...saved, revision: 2 }; return failure(409); }
    expect(body.expected_revision).toBe(2); runs = [run()]; return HttpResponse.json(runs[0], { status: 202 });
  }));
  const user = await choose(); await user.click(button('Run search')); expect(await screen.findByText(/saved search changed or was deleted/)).toBeVisible(); expect(button('Retry same run request')).toBeDisabled();
  server.use(http.get(path, () => failure(503))); await user.click(button('Reload searches before running')); expect(await screen.findByText('Saved searches could not be reloaded. Try again.')).toBeVisible();
  server.use(http.get(path, () => HttpResponse.json({ items: [saved], next_offset: null })));
  await user.click(button('Reload searches before running')); expect(await screen.findByText(/Review the saved version before running/)).toBeVisible();
  await user.click(button('Run search')); expect(await screen.findByText(/Run request confirmed/)).toBeVisible(); expect(keys[0]).not.toBe(keys[1]);
});

test('polls active discovery every five seconds, pauses hidden and stops after partial completion', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] }); runs = [run()]; const view = mount();
  await advance(50); await vi.waitFor(() => { expect(screen.getByText('Discovery queued')).toBeVisible(); }); expect(reads).toBe(1);
  await advance(4500); expect(reads).toBe(1); await advance(600); expect(reads).toBe(2);
  act(() => { focusManager.setFocused(false); }); await advance(20000); expect(reads).toBe(2);
  runs = [{ ...runs[0] as Run, status: 'running' }]; act(() => { focusManager.setFocused(true); }); await advance(5000); expect(reads).toBe(3);
  runs = [{ ...runs[0] as Run, status: 'partial', counters: { found_results: 12, new_urls: 4, known_urls: 6, skipped_results: 2, processing_total: 8, processing_ready: 1, processing_failed: 2 }, discovery: { pages_completed: 1, max_pages: 2, error_code: 'timeout' } }];
  await advance(5000); await advance(50); expect(screen.getByText('Partial discovery')).toBeVisible(); expect(screen.getByText(/Counts cover the pages completed so far/)).toBeVisible();
  expect(screen.getByText('Found: 12 · New URLs: 4 · Existing URLs: 6 · Skipped: 2')).toBeVisible(); expect(screen.getByText(/Job processing: 1 ready \/ 8 associated · 2 failed/)).toBeVisible();
  const finished = reads; await advance(20000); expect(reads).toBe(finished); view.unmount(); await advance(20000); expect(reads).toBe(finished);
});

test('confirms cancellation, handles uncertain failure and a completion race without promising rollback', async () => {
  runs = [run('running')]; let calls = 0; const deferred = gate();
  server.use(http.post(path + '/runs/:id/cancel', async ({ request, params }) => {
    calls++; expect(params.id).toBe(runs[0]?.id); expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    if (calls === 1) return failure(503); await deferred.pending; runs = [{ ...runs[0] as Run, status: 'completed' }]; return HttpResponse.json(runs[0]);
  }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Cancel discovery for Engineering' }));
  expect(await screen.findByText(/Cancellation could not be confirmed/)).toBeVisible(); const cancel = button('Cancel discovery for Engineering'); fireEvent.click(cancel); fireEvent.click(cancel);
  await waitFor(() => { expect(cancel).toBeDisabled(); }); deferred.release(); expect(await screen.findByText(/Discovery complete. Existing discoveries and shared job processing are kept/)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Cancel discovery for Engineering' })).not.toBeInTheDocument(); expect(calls).toBe(2);
});

test.each(['quota', 'rate_limited', 'disabled', 'input_limit', 'provider_failure'])("renders safe '%s' failure guidance without provider internals", async code => {
  runs = [run('failed', { discovery: { error_code: code, last_request_id: 'provider-private-detail' } })]; mount();
  expect(await screen.findByText('Discovery failed')).toBeVisible(); expect(screen.queryByText('provider-private-detail')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Cancel discovery for Engineering' })).not.toBeInTheDocument();
  expect(screen.getByText(code === 'quota' ? /search quota is exhausted/ : code === 'rate_limited' ? /temporarily rate limited/ : code === 'disabled' ? /configuration needs attention/ : code === 'input_limit' ? /could not use these results/ : /temporary failure/)).toBeVisible();
});

test('honors 429 Retry-After and retries the same request only on explicit action', async () => {
  let calls = 0, original: unknown;
  server.use(http.post(path + '/:id/runs', async ({ request }) => { calls++; const body = await request.json(); if (calls === 1) { original = body; return failure(429, '2'); } expect(body).toEqual(original); return HttpResponse.json(run(), { status: 202 }); }));
  await choose(); vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] }); fireEvent.click(button('Run search')); await advance(50);
  await vi.waitFor(() => { expect(screen.getByText(/request was rate limited/)).toBeVisible(); }); expect(button('Retry same run request')).toBeDisabled();
  await advance(1000); expect(button('Retry same run request')).toBeDisabled(); await advance(1000); await advance(50); expect(button('Retry same run request')).toBeEnabled();
  expect(calls).toBe(1); fireEvent.click(button('Retry same run request')); await advance(50); await vi.waitFor(() => { expect(screen.getByText(/Run request confirmed/)).toBeVisible(); }); expect(calls).toBe(2);
});

test('keeps stale history on errors, paginates older runs and recovers availability', async () => {
  runs = [run('completed', { discovery: { limited: true }, snapshot: { name: '<img src=x onerror=alert(1)>', definition: saved.definition, query: 'private', provider_parameters: {} } })]; let broken = false;
  server.use(http.get(path + '/provider', () => failure(503)), http.get(path + '/runs', ({ request }) => {
    if (broken) return failure(503); return HttpResponse.json(new URL(request.url).searchParams.get('offset') === '20' ? { items: [], next_offset: null } : { items: runs, next_offset: 20 });
  }));
  const user = userEvent.setup(); mount(); expect(await screen.findByText(/availability could not be checked/)).toBeVisible();
  expect(await screen.findByText(/configured page or result limit was reached/)).toBeVisible(); expect(document.querySelector('img')).toBeNull();
  await user.click(button('Older runs')); expect(await screen.findByText('No runs on this page.')).toBeVisible(); await user.click(button('Newer runs'));
  await screen.findByText('Discovery complete'); broken = true; await user.click(button('Refresh run history'));
  expect(await screen.findByText(/statuses may be out of date/)).toBeVisible(); expect(screen.getByText('Discovery complete')).toBeVisible();
  broken = false; await user.click(button('Refresh run history')); server.use(http.get(path + '/provider', () => HttpResponse.json(capability)));
  await user.click(button('Check search availability')); expect(await screen.findByText('Each run checks at most 2 pages and 20 results.')).toBeVisible();
});

test.each(['history', 'start'])('aborts %s and removes private results on session switch', async operation => {
  const deferred = gate(); let signal: AbortSignal | undefined; runs = [run('running')];
  if (operation === 'history') server.use(http.get(path + '/runs', async ({ request }) => { signal ??= request.signal; await deferred.pending; return HttpResponse.json({ items: runs, next_offset: null }); }));
  else server.use(http.post(path + '/:id/runs', async ({ request }) => { signal = request.signal; await deferred.pending; return HttpResponse.json(runs[0], { status: 202 }); }));
  const user = await choose(); if (operation === 'start') await user.click(button('Run search')); await waitFor(() => { expect(signal).toBeDefined(); });
  current = { ...owner, id: crypto.randomUUID(), email: 'other@example.org' }; runs = [];
  await act(async () => { await api.invalidateSession(); }); expect(signal?.aborted).toBe(true); deferred.release();
  expect(await screen.findByText('No saved searches yet')).toBeVisible(); expect(screen.queryByText(/Requested search:/)).not.toBeInTheDocument();
  expect(api.queryClient.getQueryData(['search-runs', owner.id, 0])).toBeUndefined();
});

test('refreshes an expired session after a forbidden run request', async () => {
  server.use(http.post(path + '/:id/runs', () => { current = null; return failure(403); }));
  const user = await choose(); await user.click(button('Run search')); expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeVisible();
  expect(screen.queryByText(/Requested search:/)).not.toBeInTheDocument();
});
