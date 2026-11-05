import { QueryClientProvider, MutationObserver, useQuery } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { server } from '../test/setup';
import { ApiError, createApiClient, sessionKey, type Session } from './api';
import type { components } from '../../generated/api';

const user: Session = {
  id: '91be5722-c850-49c5-a3bc-1ef0d5fae21e', email: 'first@example.org',
  email_verified: true, csrf_token: 'a'.repeat(64),
};
const failure: components['schemas']['ErrorEnvelope'] = {
  code: 'validation_error', message: 'Check the submitted fields.',
  field_errors: [{ field: 'email', message: 'Invalid value.' }],
  request_id: '84fb70bf-9d41-4cc4-a81b-bc031c727d78',
};
let client: ReturnType<typeof createApiClient>;
beforeEach(() => { client = createApiClient(); });
afterEach(() => { client.clearSession(); client.queryClient.clear(); vi.useRealTimers(); });

function gate() {
  let open: () => void = () => undefined;
  const promise = new Promise<void>(resolve => { open = resolve; });
  return { promise, open };
}

test('loads the generated session through React Query using a same-origin uncached request', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch');
  server.use(http.get('http://localhost/api/auth/me', ({ request }) => {
    expect(request.credentials).toBe('same-origin');
    expect(request.headers.get('accept')).toBe('application/json');
    expect(request.headers.has('x-csrf-token')).toBe(false);
    return HttpResponse.json(user);
  }));
  function SessionView() {
    const { data } = useQuery(client.currentSession);
    return <p>{data?.email ?? 'Loading'}</p>;
  }
  render(<QueryClientProvider client={client.queryClient}><SessionView /></QueryClientProvider>);
  expect(screen.getByText('Loading')).toBeVisible();
  expect(await screen.findByText(user.email)).toBeVisible();
  expect(client.queryClient.getQueryData(sessionKey)).toEqual(user);
  expect(fetch).toHaveBeenCalledWith(expect.any(URL), expect.objectContaining({
    credentials: 'same-origin', redirect: 'error', cache: 'no-store',
  }));
});

test('sends JSON and the in-memory CSRF token on writes, including a rotated token', async () => {
  const tokens: (string | null)[] = [];
  const body: components['schemas']['LoginRequest'] = { email: user.email, password: 'fixture-password' };
  server.use(http.post('http://localhost/api/auth/login', async ({ request }) => {
    expect(request.credentials).toBe('same-origin');
    expect(request.headers.get('content-type')).toBe('application/json');
    expect(await request.json()).toEqual(body);
    tokens.push(request.headers.get('x-csrf-token'));
    return HttpResponse.json(user);
  }));
  await client.request('/api/auth/login', { method: 'POST', body });
  client.acceptSession(user);
  await client.request('/api/auth/login', { method: 'POST', body });
  client.acceptSession({ ...user, csrf_token: 'b'.repeat(64) });
  await client.request('/api/auth/login', { method: 'POST', body });
  expect(tokens).toEqual([null, user.csrf_token, 'b'.repeat(64)]);
});

test.each(['https://other.example/api/private', '//other.example/api/private', '/api/../private'])(
  'rejects an out-of-boundary path before sending a request: %s', async path => {
    await expect(client.request(path as `/api/${string}`)).rejects.toThrow('same-origin /api boundary');
  },
);

test.each([
  [401, 'unauthenticated'], [422, 'validation'], [403, 'http'], [409, 'http'],
  [408, 'retryable'], [429, 'retryable'], [500, 'retryable'], [503, 'retryable'],
])('maps HTTP %i without losing field errors or request identity', async (status, kind) => {
  server.use(http.get('http://localhost/api/example', () => HttpResponse.json(failure, { status })));
  await expect(client.request('/api/example')).rejects.toMatchObject({ kind, status, detail: failure });
});

test.each([null, { ...failure, field_errors: [null] }, { ...failure, field_errors: [{ field: 'email' }] }])(
  'uses a safe fallback for a malformed error envelope', async payload => {
    server.use(http.get('http://localhost/api/example', () => HttpResponse.json(payload, { status: 502 })));
    await expect(client.request('/api/example')).rejects.toMatchObject({ kind: 'retryable', detail: undefined,
      message: 'The request could not be completed.' });
  },
);

test('preserves the existing reauthentication error action alongside the standard envelope', async () => {
  const detail: components['schemas']['ReauthenticationError'] = {
    ...failure, code: 'reauthentication_required', action: 'link_google',
  };
  server.use(http.get('http://localhost/api/auth/actions/link_google', () => HttpResponse.json(detail, { status: 403 })));
  await expect(client.request('/api/auth/actions/link_google')).rejects.toMatchObject({ kind: 'http', detail });
});

test('distinguishes invalid success JSON from a network failure and blocks redirects', async () => {
  server.use(http.get('http://localhost/api/example', () => HttpResponse.text('<html>upstream failure</html>')));
  await expect(client.request('/api/example')).rejects.toMatchObject({ kind: 'invalid_response' });
  server.use(http.get('http://localhost/api/example', () => HttpResponse.error()));
  await expect(client.request('/api/example')).rejects.toMatchObject({ kind: 'network', status: 0 });
  server.use(http.get('http://localhost/api/example', () => new HttpResponse(null,
    { status: 302, headers: { Location: 'https://other.example/private' } })));
  await expect(client.request('/api/example')).rejects.toBeInstanceOf(ApiError);
});

test('honors abort before dispatch and while a response is pending', async () => {
  const controller = new AbortController();
  controller.abort();
  await expect(client.request('/api/example', { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' });
  const started = gate(), release = gate();
  server.use(http.get('http://localhost/api/example', async () => {
    started.open(); await release.promise; return HttpResponse.json({ private: 'old' });
  }));
  const active = new AbortController();
  const pending = client.request('/api/example', { signal: active.signal }).catch((error: unknown) => error);
  await started.promise;
  active.abort();
  release.open();
  expect(await pending).toMatchObject({ name: 'AbortError' });
});

test('bounds transient read retries to three attempts with exponential delays', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  let calls = 0;
  server.use(http.get('http://localhost/api/example', () => { calls++; return HttpResponse.json(failure, { status: 503 }); }));
  const pending = client.queryClient.query({ queryKey: ['example'],
    queryFn: () => client.request('/api/example') }).catch((error: unknown) => error);
  await vi.waitFor(() => { expect(client.queryClient.getQueryState(['example'])?.fetchFailureCount).toBe(1); });
  await vi.advanceTimersByTimeAsync(1000);
  await vi.waitFor(() => { expect(client.queryClient.getQueryState(['example'])?.fetchFailureCount).toBe(2); });
  expect(calls).toBe(2);
  await vi.advanceTimersByTimeAsync(2000);
  await vi.waitFor(() => { expect(client.queryClient.getQueryState(['example'])?.status).toBe('error'); });
  expect(await pending).toMatchObject({ kind: 'retryable', status: 503 });
  expect(calls).toBe(3);
});

test('retries a network read and recovers, but never retries validation or unknown errors', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  let calls = 0;
  server.use(http.get('http://localhost/api/example', () => ++calls === 1 ? HttpResponse.error() : HttpResponse.json({ ok: true })));
  const pending = client.queryClient.query({ queryKey: ['example'], queryFn: () => client.request('/api/example') });
  await vi.waitFor(() => { expect(client.queryClient.getQueryState(['example'])?.fetchFailureCount).toBe(1); });
  await vi.advanceTimersByTimeAsync(1000);
  await vi.waitFor(() => { expect(client.queryClient.getQueryState(['example'])?.status).toBe('success'); });
  expect(await pending).toEqual({ ok: true });
  expect(calls).toBe(2);
  vi.useRealTimers();
  server.use(http.get('http://localhost/api/example', () => { calls++; return HttpResponse.json(failure, { status: 422 }); }));
  await expect(client.queryClient.query({ queryKey: ['invalid'], queryFn: () => client.request('/api/example') }))
    .rejects.toMatchObject({ kind: 'validation' });
  expect(calls).toBe(3);
  const broken = vi.fn(() => { throw new TypeError('Programming error'); });
  await expect(client.queryClient.query({ queryKey: ['broken'], queryFn: broken })).rejects.toThrow('Programming error');
  expect(broken).toHaveBeenCalledOnce();
});

test.each(['45', 'Thu, 01 Jan 2026 00:00:45 GMT'])(
  'does not shorten a Retry-After exceeding the 30 second automatic retry budget: %s', async header => {
    vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    let calls = 0;
    server.use(http.get('http://localhost/api/example', () => { calls++;
      return HttpResponse.json(failure, { status: 429, headers: { 'Retry-After': header } }); }));
    await expect(client.queryClient.query({ queryKey: ['limited'], queryFn: () => client.request('/api/example') }))
      .rejects.toMatchObject({ kind: 'retryable', retryAfterMs: 45000 });
    expect(calls).toBe(1);
  },
);

test.each(['2', 'invalid'])( 'honors bounded Retry-After and tolerates invalid headers: %s', async header => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  let calls = 0;
  server.use(http.get('http://localhost/api/example', () => ++calls === 1
    ? HttpResponse.json(failure, { status: 429, headers: { 'Retry-After': header } }) : HttpResponse.json({ ok: true })));
  const pending = client.queryClient.query({ queryKey: ['limited'], queryFn: () => client.request('/api/example') });
  await vi.waitFor(() => { expect(client.queryClient.getQueryState(['limited'])?.fetchFailureCount).toBe(1); });
  await vi.advanceTimersByTimeAsync(header === '2' ? 2000 : 1000);
  await vi.waitFor(() => { expect(client.queryClient.getQueryState(['limited'])?.status).toBe('success'); });
  expect(await pending).toEqual({ ok: true });
  expect(calls).toBe(2);
});

test('never automatically retries an unsafe mutation', async () => {
  let calls = 0;
  server.use(http.post('http://localhost/api/example', () => { calls++; return HttpResponse.json(failure, { status: 503 }); }));
  const mutation = new MutationObserver(client.queryClient, { mutationFn: () => client.request('/api/example', { method: 'POST' }) });
  await expect(mutation.mutate()).rejects.toMatchObject({ kind: 'retryable' });
  expect(calls).toBe(1);
});

test('explicit invalidation refreshes the session and identity/token changes discard private caches', async () => {
  let current = user;
  server.use(http.get('http://localhost/api/auth/me', () => HttpResponse.json(current)));
  await client.queryClient.query(client.currentSession);
  client.queryClient.setQueryData(['profile'], 'private');
  client.queryClient.setQueryData(['session', 'methods'], 'private methods');
  await client.invalidateSession();
  expect(client.queryClient.getQueryState(sessionKey)?.isInvalidated).toBe(true);
  await client.queryClient.query(client.currentSession);
  expect(client.queryClient.getQueryData(['profile'])).toBe('private');
  current = { ...user, csrf_token: 'b'.repeat(64) };
  await client.queryClient.query(client.currentSession);
  expect(client.queryClient.getQueryData(['profile'])).toBeUndefined();
  expect(client.queryClient.getQueryData(['session', 'methods'])).toBeUndefined();
  client.queryClient.setQueryData(['profile'], 'private');
  current = { ...user, id: '0b6787a9-edc1-4691-8ba1-fe8b0c1329bf' };
  await client.queryClient.query(client.currentSession);
  expect(client.queryClient.getQueryData(['profile'])).toBeUndefined();
  expect(client.queryClient.getQueryData(sessionKey)).toEqual(current);
});

test('a 401 session becomes anonymous and clears private data, while service failure stays an error', async () => {
  client.acceptSession(user);
  client.queryClient.setQueryData(['profile'], 'private');
  server.use(http.get('http://localhost/api/auth/me', () => HttpResponse.json(failure, { status: 401 })));
  expect(await client.queryClient.query(client.currentSession)).toBeNull();
  expect(client.queryClient.getQueryData(['profile'])).toBeUndefined();
  server.use(http.get('http://localhost/api/auth/me', () => HttpResponse.json(failure, { status: 503 })));
  await expect(client.queryClient.query({ ...client.currentSession, retry: false })).rejects.toMatchObject({ status: 503 });
});

test('identity replacement cancels old requests and clears both query and mutation caches', async () => {
  const started = gate(), release = gate();
  client.acceptSession(user);
  server.use(http.get('http://localhost/api/private', async () => {
    started.open(); await release.promise; return HttpResponse.json({ private: 'old' });
  }));
  await new MutationObserver(client.queryClient, { mutationFn: () => Promise.resolve('private mutation') }).mutate();
  const old = client.queryClient.query({ queryKey: ['private'], queryFn: ({ signal }) => client.request('/api/private', { signal }) })
    .catch((error: unknown) => error);
  await started.promise;
  client.acceptSession({ ...user, id: '0b6787a9-edc1-4691-8ba1-fe8b0c1329bf' });
  release.open();
  await old;
  expect(client.queryClient.getQueryData(['private'])).toBeUndefined();
  expect(client.queryClient.getMutationCache().getAll()).toEqual([]);
  client.acceptSession(null);
  expect(client.queryClient.getQueryData(sessionKey)).toBeNull();
});

test.each([204, 503])('logout clears caches and token even when its response is %i', async status => {
  client.acceptSession(user);
  client.queryClient.setQueryData(['profile'], 'private');
  const tokens: (string | null)[] = [];
  server.use(http.post('http://localhost/api/auth/logout', ({ request }) => {
    tokens.push(request.headers.get('x-csrf-token'));
    return status === 204 ? new HttpResponse(null, { status }) : HttpResponse.json(failure, { status });
  }));
  const result = client.logout();
  if (status === 204) await expect(result).resolves.toBeUndefined();
  else await expect(result).rejects.toMatchObject({ status });
  expect(client.queryClient.getQueryCache().getAll().map(query => query.queryKey)).toEqual([sessionKey]);
  expect(client.queryClient.getQueryData(sessionKey)).toBeNull();
  await client.request('/api/auth/logout', { method: 'POST' }).catch(() => undefined);
  expect(tokens).toEqual([user.csrf_token, null]);
});

test('an interrupted logout cannot clear a newer authenticated identity', async () => {
  const started = gate(), release = gate();
  client.acceptSession(user);
  server.use(http.post('http://localhost/api/auth/logout', async () => {
    started.open(); await release.promise; return new HttpResponse(null, { status: 204 });
  }));
  const pending = client.logout().catch((error: unknown) => error);
  await started.promise;
  const replacement = { ...user, csrf_token: 'b'.repeat(64) };
  client.acceptSession(replacement);
  release.open();
  expect(await pending).toMatchObject({ name: 'AbortError' });
  expect(client.queryClient.getQueryData(sessionKey)).toEqual(replacement);
});

test('mounted session consumers observe identity changes and logout immediately', async () => {
  server.use(http.get('http://localhost/api/auth/me', () => HttpResponse.json(user)));
  function SessionView() {
    const { data } = useQuery(client.currentSession);
    return <p>{data?.email ?? 'Anonymous'}</p>;
  }
  render(<QueryClientProvider client={client.queryClient}><SessionView /></QueryClientProvider>);
  expect(await screen.findByText(user.email)).toBeVisible();
  act(() => { client.acceptSession({ ...user, email: 'next@example.org' }); });
  expect(await screen.findByText('next@example.org')).toBeVisible();
  act(() => { client.clearSession(); });
  expect(await screen.findByText('Anonymous')).toBeVisible();
  expect(screen.queryByText('next@example.org')).not.toBeInTheDocument();
});
