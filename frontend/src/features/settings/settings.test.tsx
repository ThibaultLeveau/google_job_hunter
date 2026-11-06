import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterAll, afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest';
import App from '../../App';
import { api, sessionKey, type Session } from '../../shared/api';
import { server } from '../../test/setup';

const owner: Session = { id: '00000000-0000-4000-8000-000000000001', email: 'owner@example.org', email_verified: true, csrf_token: 'c'.repeat(64) };
const base = 'http://localhost/api', google = 'https://accounts.google.com/o/oauth2/v2/auth?state=fixture';
const failure = (status: number, code = 'fixture') => HttpResponse.json({ code, message: '<private server detail>', field_errors: [], request_id: owner.id }, { status, headers: { 'Retry-After': '2' } });
let current: Session | null, linked: string[], recent: boolean;
const download = vi.fn(), createURL = vi.fn<(blob: Blob) => string>(() => 'blob:fixture'), revokeURL = vi.fn();
function gate() { let release = () => {}; const pending = new Promise<void>(resolve => { release = resolve; }); return { pending, release }; }
function mount(path = '/settings') {
  window.history.replaceState(null, '', path); api.acceptSession(current);
  return render(<QueryClientProvider client={api.queryClient}><App /></QueryClientProvider>);
}
const button = (name: string) => screen.getByRole('button', { name });
beforeAll(() => {
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true; } },
    close: { configurable: true, value(this: HTMLDialogElement) { this.open = false; } },
  });
  Object.defineProperties(URL, { createObjectURL: { configurable: true, value: createURL }, revokeObjectURL: { configurable: true, value: revokeURL } });
});
afterAll(() => {
  for (const key of ['showModal', 'close']) Reflect.deleteProperty(HTMLDialogElement.prototype, key);
  for (const key of ['createObjectURL', 'revokeObjectURL']) Reflect.deleteProperty(URL, key);
});
beforeEach(() => {
  current = owner; linked = ['password']; recent = true; api.clearSession(); api.queryClient.clear();
  api.queryClient.setQueryDefaults(sessionKey, { retry: false }); download.mockClear(); createURL.mockClear(); revokeURL.mockClear();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { download(this.download, this.href); });
  server.use(http.get(`${base}/auth/me`, () => current ? HttpResponse.json(current) : failure(401)),
    http.get(`${base}/auth/methods`, () => HttpResponse.json({ methods: linked, auth_method: linked[0], authenticated_at: null, recent, recency_seconds: 300 })),
    http.get(`${base}/auth/providers`, () => HttpResponse.json({ google: true })));
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); api.clearSession(); api.queryClient.clear(); });

test('shows authoritative verification/method status and individual upload navigation without unlinking', async () => {
  current = { ...owner, email_verified: false }; mount();
  expect(await screen.findByText('Your email is not verified.')).toBeVisible();
  expect(screen.getByRole('link', { name: 'Open email verification' })).toHaveAttribute('href', '/verify-email');
  expect(screen.getByText('Password: available')).toBeVisible(); expect(screen.getByText('Google: not linked')).toBeVisible();
  expect(screen.getByText(/Matching email addresses never merge accounts/)).toBeVisible();
  expect(screen.getByRole('link', { name: 'Manage or remove an individual upload' })).toHaveAttribute('href', '/profile');
  expect(screen.getByText(/Encrypted backups and external providers/)).toBeVisible();
  expect(screen.queryByRole('button', { name: /unlink/i })).not.toBeInTheDocument();
});

test('keeps settings loading explicit and retries failed method/provider checks', async () => {
  const wait = gate(); let calls = 0;
  server.use(http.get(`${base}/auth/methods`, async () => { calls++; if (calls === 1) { await wait.pending; return failure(503); } return HttpResponse.json({ methods: [], recent: false }); }),
    http.get(`${base}/auth/providers`, () => failure(503)));
  const user = userEvent.setup(); mount(); expect(await screen.findByText('Loading account settings')).toBeVisible();
  wait.release(); await user.click(await screen.findByRole('button', { name: 'Retry account settings' }));
  expect(await screen.findByText(/Sign-in methods are unknown/)).toBeVisible();
  expect(screen.getByText('Password: not set')).toBeVisible();
  server.use(http.get(`${base}/auth/providers`, () => HttpResponse.json({ google: false })));
  await user.click(button('Retry Google availability'));
  expect(await screen.findByText('Google is unavailable on this service.')).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Link Google' })).not.toBeInTheDocument();
});

test('starts explicit Google linking with CSRF and focuses the validated authorization link', async () => {
  let calls = 0; const wait = gate();
  server.use(http.post(`${base}/auth/google/link`, async ({ request }) => {
    calls++; expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token); expect(await request.json()).toEqual({ action: 'link_google' });
    await wait.pending; return HttpResponse.json({ authorization_url: google });
  }));
  mount(); const link = await screen.findByRole('button', { name: 'Link Google' }); expect(calls).toBe(0);
  fireEvent.click(link); fireEvent.click(link); await waitFor(() => { expect(calls).toBe(1); });
  expect(screen.getByText('Working on your request…')).toBeVisible(); wait.release();
  const continueLink = await screen.findByRole('link', { name: 'Continue to Google' });
  expect(continueLink).toHaveAttribute('href', google); expect(continueLink).toHaveFocus();
});

test.each(['javascript:alert(1)', 'https://evil.example.org/', 'https://user@accounts.google.com/', 'not-a-url'])('rejects unsafe authorization target %s', async url => {
  server.use(http.post(`${base}/auth/google/link`, () => HttpResponse.json({ authorization_url: url })));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Link Google' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('The request did not complete.'); expect(screen.queryByRole('link', { name: 'Continue to Google' })).not.toBeInTheDocument();
});

test.each([
  ['google_link=linked', 'Google returned.'], ['google_link=conflict', 'That identity may belong to another account.'],
  ['google_link=cancelled', 'Google was cancelled.'], ['google_link=reauthentication_required', 'Authentication is too old.'],
  ['google_reauthenticate=reauthenticated', 'Authentication returned.'], ['google_link=unknown', 'Google did not complete.'],
  ['google_link=linked&google_link=linked', 'Google did not complete.'],
])('consumes a callback safely, checks actual methods and never submits an action: %s', async (fragment, message) => {
  linked = fragment === 'google_link=linked' ? ['password', 'google'] : ['password'];
  mount(`/login?code=private-query#${fragment}&action=delete_account`);
  expect(await screen.findByText(new RegExp(message.replaceAll('.', '\\.')))).toBeVisible();
  expect(window.location.pathname).toBe('/settings'); expect(window.location.hash + window.location.search).toBe('');
  expect(JSON.stringify(window.history.state)).not.toContain('private-query');
  expect(screen.getByText(linked.includes('google') ? 'Google: linked' : 'Google: not linked')).toBeVisible();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(download).not.toHaveBeenCalled();
});

async function requireProof(user: ReturnType<typeof userEvent.setup>, target = 'Download personal data') {
  server.use(http.post(`${base}/v1/account/export`, () => failure(403, 'reauthentication_required')),
    http.post(`${base}/auth/google/link`, () => failure(403, 'reauthentication_required')));
  mount(); await user.click(await screen.findByRole('button', { name: target }));
  expect(await screen.findByRole('heading', { name: 'Confirm your identity' })).toBeVisible();
}

test('clears password proof on submission, preserves the session on a wrong password and requires an explicit retry', async () => {
  const user = userEvent.setup(); let calls = 0;
  server.use(http.post(`${base}/auth/reauthenticate/password`, async ({ request }) => {
    expect(await request.json()).toEqual({ action: 'link_google', password: 'private-proof' }); calls++;
    return calls === 1 ? failure(401) : HttpResponse.json({ status: 'ready', action: 'link_google' });
  }));
  await requireProof(user, 'Link Google'); const input = screen.getByLabelText(/^Current password/); expect(input).toHaveFocus();
  await user.type(input, 'private-proof'); await user.keyboard('{Enter}');
  expect(await screen.findByText('Your password was not accepted. Try again.')).toHaveFocus(); expect(input).toHaveValue('');
  expect(api.queryClient.getQueryData(sessionKey)).toEqual(owner);
  expect(input).toHaveAccessibleDescription('Your password was not accepted. Try again.'); expect(input).toHaveAttribute('aria-invalid', 'true');
  await user.type(input, 'private-proof'); await user.keyboard('{Enter}');
  expect(await screen.findByText('Identity confirmed. Try your action again.')).toBeVisible();
  expect(screen.queryByLabelText(/^Current password/)).not.toBeInTheDocument(); expect(screen.queryByRole('link', { name: 'Continue to Google' })).not.toBeInTheDocument();
  expect(JSON.stringify(api.queryClient.getMutationCache().getAll().map(item => item.state))).not.toContain('private-proof');
  server.use(http.post(`${base}/auth/google/link`, () => HttpResponse.json({ authorization_url: google })));
  await user.click(button('Link Google')); expect(await screen.findByRole('link', { name: 'Continue to Google' })).toBeVisible();
});

test('uses an existing Google identity for proof and supports cancellation without continuing the operation', async () => {
  linked = ['google']; const user = userEvent.setup();
  server.use(http.post(`${base}/auth/google/reauthenticate`, async ({ request }) => {
    expect(await request.json()).toEqual({ action: 'export_data' }); return HttpResponse.json({ authorization_url: google });
  }));
  await requireProof(user); expect(screen.queryByLabelText(/^Current password/)).not.toBeInTheDocument();
  await user.click(button('Confirm with Google')); expect(await screen.findByRole('link', { name: 'Continue to Google' })).toBeVisible();
  await user.click(button('Cancel identity confirmation')); expect(screen.queryByRole('link', { name: 'Continue to Google' })).not.toBeInTheDocument();
  expect(await screen.findByText('Identity confirmation cancelled.')).toHaveFocus(); expect(download).not.toHaveBeenCalled();
});

test('downloads JSON once and revokes the object URL without putting the export in the query or mutation cache', async () => {
  const data = { owner: { id: owner.id }, documents: [{ text: 'private-export-été' }] }; const wait = gate(); let calls = 0;
  server.use(http.post(`${base}/v1/account/export`, async ({ request }) => {
    calls++; expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token); expect(await request.text()).toBe(''); await wait.pending; return HttpResponse.json(data);
  }));
  mount(); const control = await screen.findByRole('button', { name: 'Download personal data' });
  fireEvent.click(control); fireEvent.click(control); await waitFor(() => { expect(calls).toBe(1); });
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }); wait.release();
  await vi.waitFor(() => { expect(download).toHaveBeenCalledWith('job-hunter-personal-data.json', 'blob:fixture'); });
  const blob = createURL.mock.calls[0]?.[0]; expect(blob?.type).toBe('application/json');
  expect(JSON.stringify(api.queryClient.getMutationCache().getAll().map(item => item.state))).not.toContain('private-export');
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); }); expect(revokeURL).toHaveBeenCalledWith('blob:fixture');
});

test.each([413, 503, 0, 403])('keeps an export %i failure safe and retryable without leaking server text', async status => {
  server.use(http.post(`${base}/v1/account/export`, () => status ? failure(status) : HttpResponse.error()));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Download personal data' }));
  expect(await screen.findByRole('alert')).toHaveFocus(); expect(document.body.textContent).not.toContain('private server detail'); expect(download).not.toHaveBeenCalled();
  expect(button('Download personal data')).toBeEnabled();
});

test('honors Retry-After and prevents retries before the deadline', async () => {
  let calls = 0; server.use(http.post(`${base}/v1/account/export`, () => { calls++; return failure(429); }));
  mount(); const control = await screen.findByRole('button', { name: 'Download personal data' });
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] }); fireEvent.click(control);
  await vi.waitFor(() => { expect(screen.getByText('Try again in 2 seconds.')).toBeVisible(); });
  fireEvent.click(control); expect(calls).toBe(1); expect(control).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); }); expect(control).toBeEnabled();
});

test('requires intentional deletion and restores keyboard focus after cancellation or Escape', async () => {
  const user = userEvent.setup(); mount(); const opener = await screen.findByRole('button', { name: 'Delete my account' });
  await user.click(opener); expect(screen.getByRole('dialog')).toHaveAccessibleName('Permanently delete your account?');
  expect(button('Cancel')).toHaveFocus(); expect(button('Permanently delete account')).toBeDisabled();
  await user.click(button('Cancel')); expect(opener).toHaveFocus(); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await user.click(opener); fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true })); expect(opener).toHaveFocus();
  await user.click(opener); await user.type(screen.getByLabelText('Type DELETE to confirm'), 'delete');
  expect(button('Permanently delete account')).toBeDisabled();
  const input = screen.getByLabelText('Type DELETE to confirm'); await user.clear(input); await user.type(input, 'DELETE');
  button('Permanently delete account').focus(); await user.tab(); expect(input).toHaveFocus();
  await user.tab({ shift: true }); expect(button('Permanently delete account')).toHaveFocus();
});

test('deletes with explicit intent, blocks duplicate submissions and immediately clears private caches', async () => {
  const user = userEvent.setup(), wait = gate(); let calls = 0;
  server.use(http.post(`${base}/v1/account/delete`, async ({ request }) => {
    calls++; expect(await request.json()).toEqual({ intent: 'delete_account' }); expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    await wait.pending; current = null; return HttpResponse.json({ status: 'deleting' }, { status: 202 });
  }));
  mount(); await user.click(await screen.findByRole('button', { name: 'Delete my account' }));
  api.queryClient.setQueryData(['private-record'], 'private-cache'); await user.type(screen.getByLabelText('Type DELETE to confirm'), 'DELETE');
  await user.keyboard('{Enter}{Enter}'); await waitFor(() => { expect(calls).toBe(1); });
  fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true })); expect(screen.getByRole('dialog')).toBeVisible(); expect(button('Cancel')).toBeDisabled(); fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab' }); expect(screen.getByRole('dialog')).toHaveFocus(); wait.release();
  expect(await screen.findByText('Account deletion accepted. You are signed out. Private cleanup will continue.')).toBeVisible();
  expect(window.location.pathname).toBe('/login'); expect(api.queryClient.getQueryData(sessionKey)).toBeNull();
  expect(api.queryClient.getQueryData(['private-record'])).toBeUndefined();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(JSON.stringify(api.queryClient.getMutationCache().getAll().map(item => item.state))).not.toContain('private-cache');
});

test.each([403, 0])('keeps a failed deletion intentional after %i', async status => {
  const user = userEvent.setup(); server.use(http.post(`${base}/v1/account/delete`, () => status ? failure(status, 'reauthentication_required') : HttpResponse.error()));
  mount(); await user.click(await screen.findByRole('button', { name: 'Delete my account' }));
  await user.type(screen.getByLabelText('Type DELETE to confirm'), 'DELETE'); await user.keyboard('{Enter}');
  await waitFor(() => { expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); });
  expect(api.queryClient.getQueryData(sessionKey)).toEqual(owner);
  if (status) expect(screen.getByLabelText(/^Current password/)).toHaveFocus();
  else expect(screen.getByRole('alert')).toHaveTextContent('Deletion could not be confirmed.');
  await user.click(button('Delete my account')); expect(screen.getByLabelText('Type DELETE to confirm')).toHaveValue('');
});

test.each(['methods', 'export'])('clears private state safely on session loss during %s', async target => {
  server.use(target === 'methods' ? http.get(`${base}/auth/methods`, () => failure(401)) : http.post(`${base}/v1/account/export`, () => failure(401)));
  const user = userEvent.setup(); mount(); api.queryClient.setQueryData(['private-record'], 'private');
  if (target === 'export') await user.click(await screen.findByRole('button', { name: 'Download personal data' }));
  expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeVisible();
  expect(api.queryClient.getQueryData(['private-record'])).toBeUndefined(); expect(api.queryClient.getQueryData(sessionKey)).toBeNull();
});

test.each(['unmount', 'logout', 'switch'])('discards a delayed export after %s', async event => {
  const wait = gate(); let calls = 0;
  server.use(http.post(`${base}/v1/account/export`, async () => { calls++; await wait.pending; return HttpResponse.json({ private: 'old-owner' }); }));
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Download personal data' })); await waitFor(() => { expect(calls).toBe(1); });
  if (event === 'unmount') fireEvent.click(screen.getByRole('link', { name: 'Jobs' }));
  else act(() => { current = event === 'logout' ? null : { ...owner, id: '00000000-0000-4000-8000-000000000002' }; api.acceptSession(current); });
  await act(async () => { wait.release(); await wait.pending; }); expect(download).not.toHaveBeenCalled();
});
