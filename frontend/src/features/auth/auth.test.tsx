import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import App from '../../App';
import { api, sessionKey, type Session } from '../../shared/api';
import { server } from '../../test/setup';

const owner: Session = { id: 'f8eabf03-4823-47ec-9b28-fe03a2cc33b6', email: 'reader@example.org',
  email_verified: false, csrf_token: 'c'.repeat(64) };
const secret = '  password with safe spaces  ';
const token = 't'.repeat(43);
let current: Session | null;
const failure = (status: number, fields: { field: string; message: string }[] = [], headers = {}) =>
  HttpResponse.json({ code: 'fixture', message: 'Request failed.', field_errors: fields,
    request_id: 'bcb2f310-f4a5-4a2d-9871-a220704e5da8' }, { status, headers });
beforeEach(() => {
  sessionStorage.clear();
  server.use(http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: false })));
  api.queryClient.setQueryDefaults(['auth-providers'], { retry: false });
  current = null; api.clearSession(); api.queryClient.clear();
  api.queryClient.setQueryDefaults(sessionKey, { retry: false });
  server.use(http.get('http://localhost/api/auth/me', () => current ? HttpResponse.json(current) : failure(401)));
});
afterEach(() => { api.clearSession(); api.queryClient.clear(); api.queryClient.setQueryDefaults(sessionKey, {}); vi.useRealTimers(); });
function mount(path: string) {
  window.history.replaceState(null, '', path);
  return render(<QueryClientProvider client={api.queryClient}><App /></QueryClientProvider>);
}
async function fill(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByRole('textbox', { name: 'Email address' }), owner.email);
  await user.type(screen.getByLabelText(/^Password/), secret);
}
function login(verified = false) {
  server.use(http.post('http://localhost/api/auth/login', async ({ request }) => {
    expect(await request.json()).toEqual({ email: owner.email, password: secret });
    current = { ...owner, email_verified: verified }; return HttpResponse.json(current);
  }));
}

test('registers through the typed API without claiming verification and retains only email across forms', async () => {
  const user = userEvent.setup(); let calls = 0;
  server.use(http.post('http://localhost/api/auth/register', async ({ request }) => {
    calls++; expect(await request.json()).toEqual({ email: owner.email, password: secret });
    return HttpResponse.json({ status: 'accepted' }, { status: 202 });
  }));
  mount('/register?returnTo=/jobs'); await fill(user);
  expect(screen.getByLabelText(/^Password/)).toHaveAttribute('autocomplete', 'new-password');
  await user.keyboard('{Enter}');
  expect(await screen.findByText(/If a new account was created/)).toHaveTextContent('Sign in to check your account and complete verification.');
  expect(screen.getByRole('link', { name: 'Sign in to continue' })).toHaveFocus();
  expect(calls).toBe(1); expect(api.queryClient.getQueryData(sessionKey)).toBeNull();
  expect(JSON.stringify(api.queryClient.getMutationCache().getAll().map(item => item.state))).not.toContain(secret);
  await user.click(screen.getByRole('link', { name: 'Sign in to continue' }));
  expect(screen.getByRole('textbox', { name: 'Email address' })).toHaveValue(owner.email);
  expect(screen.getByLabelText(/^Password/)).toHaveValue('');
  expect(new URLSearchParams(window.location.search).get('returnTo')).toBe('/jobs');
});

test('supports password visibility, keyboard login and a safe return path', async () => {
  const user = userEvent.setup(); login(true); mount('/login?returnTo=/jobs'); await fill(user);
  const password = screen.getByLabelText(/^Password/);
  expect(password).toHaveAttribute('autocomplete', 'current-password');
  await user.click(screen.getByRole('button', { name: 'Show password' }));
  expect(password).toHaveAttribute('type', 'text');
  expect(screen.getByRole('button', { name: 'Hide password' })).toHaveAttribute('aria-pressed', 'true');
  await user.click(screen.getByRole('button', { name: 'Hide password' }));
  expect(password).toHaveAttribute('type', 'password');
  password.focus(); await user.keyboard('{Enter}');
  expect(await screen.findByText('Jobs tools are not available yet')).toBeVisible();
  expect(window.location.pathname).toBe('/jobs');
});

test.each(['/login', '/register'])('rejects missing or malformed input before sending %s', async path => {
  const user = userEvent.setup(); let calls = 0;
  server.use(http.post(`http://localhost/api/auth${path}`, () => { calls++; return failure(422); }));
  mount(path);
  await user.click(screen.getByRole('button', { name: path === '/login' ? 'Sign in' : 'Create account' }));
  expect(screen.getByRole('textbox', { name: 'Email address' })).toBeInvalid();
  await user.type(screen.getByRole('textbox', { name: 'Email address' }), 'not-an-email');
  await user.type(screen.getByLabelText(/^Password/), secret); await user.keyboard('{Enter}');
  expect(calls).toBe(0);
});

test.each([401, 422, 503, 0])('renders the %i failure safely, preserves email and clears the password', async status => {
  const user = userEvent.setup(); const storage = vi.spyOn(Storage.prototype, 'setItem');
  server.use(http.post('http://localhost/api/auth/login', () => status === 0 ? HttpResponse.error()
    : failure(status, status === 422 ? [{ field: 'email', message: '<img src=x> is invalid.' }] : [])));
  mount('/login'); await fill(user);
  await user.click(screen.getByRole('button', { name: 'Show password' }));
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
  expect(await screen.findByText(/Your password was cleared/)).toBeVisible();
  expect(screen.getByRole('textbox', { name: 'Email address' })).toHaveValue(owner.email);
  expect(screen.getByLabelText(/^Password/)).toHaveValue('');
  expect(screen.getByLabelText(/^Password/)).toHaveAttribute('type', 'password');
  expect(storage).not.toHaveBeenCalled(); expect(document.querySelector('img')).toBeNull();
  if (status === 422) {
    expect(screen.getByRole('textbox', { name: 'Email address' })).toHaveAccessibleDescription('Error: <img src=x> is invalid.');
    expect(screen.getByRole('textbox', { name: 'Email address' })).toHaveFocus();
  } else expect(screen.getByRole('alert')).toHaveFocus();
});

test.each(['login', 'resend-verification'])('honors Retry-After for %s without automatic mutation retries', async endpoint => {
  const user = userEvent.setup(); let calls = 0;
  if (endpoint !== 'login') current = owner;
  server.use(http.post(`http://localhost/api/auth/${endpoint}`, () => { calls++; return failure(429, [], { 'Retry-After': '2' }); }));
  mount(endpoint === 'login' ? '/login' : '/verify-email');
  if (endpoint === 'login') await fill(user);
  const label = endpoint === 'login' ? 'Sign in' : 'Send a new verification email';
  const submit = await screen.findByRole('button', { name: label });
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  fireEvent.click(submit);
  await vi.waitFor(() => { expect(screen.getByText(/Try again in/)).toBeVisible(); });
  expect(screen.getByRole('button', { name: label })).toBeDisabled();
  fireEvent.click(submit); expect(calls).toBe(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByRole('button', { name: label })).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByRole('button', { name: label })).toBeEnabled(); expect(calls).toBe(1);
});

test('blocks duplicate keyboard submissions while login is pending', async () => {
  const user = userEvent.setup(); let calls = 0; let release: () => void = () => undefined;
  const pending = new Promise<void>(resolve => { release = resolve; });
  server.use(http.post('http://localhost/api/auth/login', async () => {
    calls++; await pending; current = owner; return HttpResponse.json(current);
  }));
  mount('/login'); await fill(user);
  await user.keyboard('{Enter}{Enter}'); fireEvent.submit(screen.getByRole('form', { name: 'Sign in' }));
  expect(screen.getByRole('button', { name: 'Signing in…' })).toBeDisabled();
  release(); expect(await screen.findByText('Verify your email to continue')).toBeVisible(); expect(calls).toBe(1);
});

test.each(['/profile', '/searches', '/jobs'])('withholds %s tools from unverified accounts while verification stays reachable', async path => {
  current = owner; const user = userEvent.setup(); mount(path);
  expect(await screen.findByText('Verify your email to continue')).toBeVisible();
  expect(screen.queryByText(/tools are not available yet/)).not.toBeInTheDocument();
  await user.click(screen.getByRole('link', { name: 'Open email verification' }));
  expect(await screen.findByRole('button', { name: 'Send a new verification email' })).toBeEnabled();
  await user.click(screen.getByRole('link', { name: 'Settings' }));
  expect(await screen.findByText('Settings tools are not available yet')).toBeVisible();
});

test('clears a verification URL, keeps its token through inline login and confirms actual server state', async () => {
  const user = userEvent.setup(); login(); let calls = 0;
  server.use(http.post('http://localhost/api/auth/verify', async ({ request }) => {
    calls++; expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    expect(await request.json()).toEqual({ token }); current = { ...owner, email_verified: true };
    return HttpResponse.json({ status: 'accepted' }, { status: 202 });
  }));
  mount(`/verify-email#token=${token}`);
  await waitFor(() => { expect(window.location.hash).toBe(''); });
  expect(await screen.findByText(/Sign in to the account for this verification email/)).toBeVisible();
  await fill(user); await user.keyboard('{Enter}');
  await user.click(await screen.findByRole('button', { name: 'Verify email' }));
  expect(await screen.findByText('Your account email is verified.')).toBeVisible();
  expect(screen.getByText('Your account email is verified.')).toHaveFocus();
  expect(calls).toBe(1); expect(JSON.stringify(window.history.state)).not.toContain(token);
  await user.click(screen.getByRole('link', { name: 'Continue to your profile' }));
  expect(await screen.findByText('Profile tools are not available yet')).toBeVisible();
});

test('treats expired, replayed or foreign token acceptance as unverified and offers resend', async () => {
  current = owner; const user = userEvent.setup(); let resent = 0;
  server.use(http.post('http://localhost/api/auth/verify', () => HttpResponse.json({ status: 'accepted' }, { status: 202 })),
    http.post('http://localhost/api/auth/resend-verification', () => { resent++; return HttpResponse.json({ status: 'accepted' }, { status: 202 }); }));
  mount(`/verify-email#token=${token}`);
  await user.click(await screen.findByRole('button', { name: 'Verify email' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('It may be expired, already used or for another account.');
  expect(screen.queryByRole('button', { name: 'Verify email' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Send a new verification email' }));
  expect(await screen.findByText(/Request received. Check your inbox/)).toHaveTextContent('Your email is not verified yet.');
  expect(resent).toBe(1); expect(screen.queryByText('Your account email is verified.')).not.toBeInTheDocument();
});

test.each(['#token=short', `#token=${token}&token=${token}`, '?token=private-query-fixture'])('rejects malformed links and removes all URL secrets: %s', async suffix => {
  current = owner; mount(`/verify-email${suffix}`);
  expect(await screen.findByRole('button', { name: 'Send a new verification email' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Verify email' })).not.toBeInTheDocument();
  expect(window.location.search + window.location.hash).toBe('');
});

test('shows current verification without resubmitting a replayed link for an already verified account', async () => {
  current = { ...owner, email_verified: true }; mount(`/verify-email#token=${token}`);
  expect(await screen.findByText('Your account email is verified.')).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Verify email' })).not.toBeInTheDocument(); expect(window.location.hash).toBe('');
});

test.each([401, 503])('handles verification failure %i without claiming success or retaining the URL', async status => {
  current = owner; const user = userEvent.setup();
  server.use(http.post('http://localhost/api/auth/verify', () => failure(status)));
  mount(`/verify-email#token=${token}`);
  await user.click(await screen.findByRole('button', { name: 'Verify email' }));
  if (status === 401) expect(await screen.findByRole('form', { name: 'Sign in' })).toBeVisible();
  else expect(await screen.findByRole('alert')).toHaveTextContent('Reopen your email link or request a new one.');
  expect(window.location.hash).toBe(''); expect(screen.queryByText('Your account email is verified.')).not.toBeInTheDocument();
});

test('requires a successful session refresh before claiming verification and can retry that refresh', async () => {
  current = owner; const user = userEvent.setup();
  server.use(http.post('http://localhost/api/auth/verify', () => {
    server.use(http.get('http://localhost/api/auth/me', () => failure(503)));
    return HttpResponse.json({ status: 'accepted' }, { status: 202 });
  }));
  mount(`/verify-email#token=${token}`);
  await user.click(await screen.findByRole('button', { name: 'Verify email' }));
  expect(await screen.findByText('We couldn’t check your account')).toBeVisible();
  server.use(http.get('http://localhost/api/auth/me', () => HttpResponse.json({ ...owner, email_verified: true })));
  await user.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByText('Your account email is verified.')).toBeVisible();
});


test('associates server password validation with the registration field', async () => {
  const user = userEvent.setup();
  server.use(http.post('http://localhost/api/auth/register', () => failure(422, [{ field: 'password', message: 'Invalid password length.' }])));
  mount('/register'); await fill(user); await user.keyboard('{Enter}');
  const password = screen.getByLabelText(/^Password/);
  await waitFor(() => { expect(password).toHaveAttribute('aria-invalid', 'true'); });
  expect(password).toHaveAccessibleDescription(/Invalid password length/); expect(password).toHaveFocus(); expect(password).toHaveValue('');
});

test.each([401, 503])('handles resend failure %i without claiming delivery or verification', async status => {
  current = owner; const user = userEvent.setup();
  server.use(http.post('http://localhost/api/auth/resend-verification', () => failure(status)));
  mount('/verify-email'); await user.click(await screen.findByRole('button', { name: 'Send a new verification email' }));
  if (status === 401) expect(await screen.findByRole('form', { name: 'Sign in' })).toBeVisible();
  else expect(await screen.findByRole('alert')).toHaveTextContent('The request could not be completed.');
  expect(screen.queryByText(/Request received. Check your inbox/)).not.toBeInTheDocument();
});

test('accepts another fragment link in the same page after clearing the previous URL', async () => {
  current = owner; const user = userEvent.setup(); const seen: unknown[] = [];
  server.use(http.post('http://localhost/api/auth/verify', async ({ request }) => {
    seen.push(await request.json()); return HttpResponse.json({ status: 'accepted' }, { status: 202 });
  }));
  mount(`/verify-email#token=${token}`);
  await user.click(await screen.findByRole('button', { name: 'Verify email' }));
  expect(await screen.findByRole('alert')).toBeVisible();
  act(() => {
    window.history.pushState(null, '', `/verify-email#token=${'n'.repeat(43)}`);
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await user.click(await screen.findByRole('button', { name: 'Verify email' }));
  await waitFor(() => { expect(seen).toEqual([{ token }, { token: 'n'.repeat(43) }]); });
  expect(window.location.hash).toBe('');
});

test('offers Google only after configured availability is confirmed, with an explicit retry on failure', async () => {
  const user = userEvent.setup(); let release: () => void = () => undefined;
  const pending = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get('http://localhost/api/auth/providers', async () => { await pending; return failure(503); }));
  mount('/login'); expect(screen.getByText('Checking sign-in options…')).toBeVisible();
  expect(screen.queryByRole('link', { name: 'Sign in with Google' })).not.toBeInTheDocument();
  release(); await screen.findByText('Google sign-in availability could not be checked.');
  server.use(http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: true })));
  await user.click(screen.getByRole('button', { name: 'Retry Google availability' }));
  expect(await screen.findByRole('link', { name: 'Sign in with Google' })).toHaveAttribute('href', '/api/auth/google/start');
});

test.each(['/jobs?filter=saved', '//outside.example', '/login', null])('stores only a safe Google return path: %s', async target => {
  server.use(http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: true })));
  mount(`/login${target ? `?returnTo=${encodeURIComponent(target)}` : ''}`);
  const link = await screen.findByRole('link', { name: 'Sign in with Google' });
  link.addEventListener('click', event => { event.preventDefault(); }); fireEvent.click(link);
  expect(sessionStorage.getItem('job-hunter.google-return')).toBe(target?.startsWith('/jobs') ? target : '/profile');
});

test('keeps Google navigation available when storage is blocked and explains leaving a verification link', async () => {
  server.use(http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: true })));
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Unavailable'); });
  mount(`/verify-email#token=${token}`);
  const link = await screen.findByRole('link', { name: 'Sign in with Google' });
  link.addEventListener('click', event => { event.preventDefault(); }); fireEvent.click(link);
  expect(screen.getByText(/Reopen your verification email afterward/)).toBeVisible();
});

test.each([
  ['cancelled', 'Google sign-in was cancelled'], ['linking_required', 'Use your existing sign-in method'],
  ['signed_in', 'Google sign-in did not complete'], ['<img src=x>', 'Google sign-in did not complete'],
  ['failed&google=signed_in', 'Google sign-in did not complete'],
])('handles callback %s without trusting the URL to establish a session', async (outcome, title) => {
  sessionStorage.setItem('job-hunter.google-return', '/jobs');
  mount(`/login?code=private-code&state=private-state#google=${outcome}`);
  expect(await screen.findByText(title)).toBeVisible();
  await waitFor(() => { expect(screen.getByText(title).closest('[tabindex]')).toHaveFocus(); });
  expect(window.location.hash).toBe(''); expect(window.location.search).not.toMatch(/private/);
  expect(sessionStorage.length).toBe(0); expect(document.querySelector('img')).toBeNull();
  if (outcome === 'linking_required') {
    expect(screen.getByText(/matching email addresses never connect accounts/)).toBeVisible();
    expect(screen.getByRole('link', { name: 'Account settings' })).toHaveAttribute('href', '/settings');
  }
});

test('refreshes a successful Google callback before opening the validated saved workspace', async () => {
  sessionStorage.setItem('job-hunter.google-return', '/jobs?filter=saved'); current = { ...owner, email_verified: true };
  mount('/login#google=signed_in');
  expect(await screen.findByText('Jobs tools are not available yet')).toBeVisible();
  expect(window.location.pathname + window.location.search).toBe('/jobs?filter=saved');
  expect(window.location.hash).toBe('');
});

test('falls back safely when callback storage is unavailable and retries an unavailable session', async () => {
  const user = userEvent.setup();
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Unavailable'); });
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('Unavailable'); });
  server.use(http.get('http://localhost/api/auth/me', () => failure(503)));
  mount('/login?returnTo=https://outside.example#google=signed_in');
  expect(await screen.findByText('We couldn’t confirm Google sign-in')).toBeVisible();
  current = { ...owner, email_verified: true };
  server.use(http.get('http://localhost/api/auth/me', () => HttpResponse.json(current)));
  await user.click(screen.getByRole('button', { name: 'Check session again' }));
  expect(await screen.findByText('Profile tools are not available yet')).toBeVisible();
});

test('handles a later callback in the same page and preserves the existing account after cancellation', async () => {
  const user = userEvent.setup(); current = { ...owner, email_verified: true };
  mount('/login#google=cancelled');
  expect(await screen.findByRole('link', { name: 'Return to your workspace' })).toBeVisible();
  act(() => { window.history.pushState(null, '', '/login#google=linking_required'); window.dispatchEvent(new PopStateEvent('popstate')); });
  expect(await screen.findByText('Use your existing sign-in method')).toBeVisible();
  expect(window.location.hash).toBe('');
  await user.click(screen.getByRole('link', { name: 'Return to your workspace' }));
  expect(await screen.findByText('Profile tools are not available yet')).toBeVisible();
});

test.each(['known@example.org', 'unknown@example.org'])('makes the same generic recovery request for %s', async email => {
  const user = userEvent.setup(); let calls = 0;
  server.use(http.post('http://localhost/api/auth/forgot-password', async ({ request }) => {
    calls++; expect(await request.json()).toEqual({ email }); return HttpResponse.json({ status: 'accepted' }, { status: 202 });
  }));
  mount('/login'); await user.type(screen.getByRole('textbox', { name: 'Email address' }), email);
  await user.click(screen.getByRole('link', { name: 'Forgot your password?' }));
  expect(screen.getByRole('textbox', { name: 'Email address' })).toHaveValue(email);
  await user.click(screen.getByRole('button', { name: 'Send reset link' }));
  expect(await screen.findByText(/If this email belongs to a password account/)).toHaveTextContent('Links last one hour.');
  expect(screen.getByRole('link', { name: 'Return to sign in' })).toHaveFocus();
  await user.click(screen.getByRole('link', { name: 'Return to sign in' }));
  expect(screen.getByRole('textbox', { name: 'Email address' })).toHaveValue(email); expect(calls).toBe(1);
});

test.each(['', '#token=short', `#token=${token}&token=${token}`, '?token=private-query'])('cleans unusable reset links and offers replacement: %s', async suffix => {
  mount(`/reset-password${suffix}`);
  expect(await screen.findByText('Reset link unavailable')).toBeVisible();
  expect(screen.getByText(/Links expire after one hour/)).toBeVisible();
  expect(screen.getByRole('link', { name: 'Request a new reset link' })).toHaveAttribute('href', '/forgot-password');
  expect(window.location.hash + window.location.search).toBe('');
  expect(screen.queryByRole('button', { name: 'Reset password' })).not.toBeInTheDocument();
});

test.each([true, false])('submits a reset without storing credentials and handles generic acceptance (valid=%s)', async valid => {
  const user = userEvent.setup(); current = owner; let calls = 0; let release: () => void = () => undefined;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const storage = vi.spyOn(Storage.prototype, 'setItem');
  server.use(http.post('http://localhost/api/auth/reset-password', async ({ request }) => {
    calls++; expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    expect(await request.json()).toEqual({ token, password: secret }); await pending;
    if (valid) current = null;
    return HttpResponse.json({ status: 'accepted' }, { status: 202 });
  }));
  mount(`/reset-password#token=${token}`);
  await screen.findByRole('button', { name: 'Account' });
  api.queryClient.setQueryData(['private-profile'], 'private fixture');
  const password = screen.getByLabelText(/^New password/);
  expect(password).toHaveAttribute('autocomplete', 'new-password');
  await user.type(password, secret); await user.click(screen.getByRole('button', { name: 'Show password' }));
  expect(password).toHaveAttribute('type', 'text');
  await user.click(screen.getByRole('button', { name: 'Hide password' })); password.focus(); await user.keyboard('{Enter}{Enter}');
  fireEvent.submit(screen.getByRole('form', { name: 'Reset password' }));
  expect(screen.getByRole('button', { name: 'Sending request…' })).toBeDisabled(); release();
  expect(await screen.findByText(/An expired or already-used link cannot change your password/)).toBeVisible();
  expect(calls).toBe(1); expect(storage).not.toHaveBeenCalled();
  expect(window.location.hash + window.location.search).toBe(''); expect(JSON.stringify(window.history.state)).not.toContain(token);
  expect(JSON.stringify(api.queryClient.getMutationCache().getAll().map(item => item.state))).not.toMatch(new RegExp(token + '|' + secret.trim()));
  if (valid) {
    expect(api.queryClient.getQueryData(['private-profile'])).toBeUndefined();
    await user.click(screen.getByRole('link', { name: 'Return to sign in' }));
    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeVisible();
    expect(screen.getByLabelText(/^Password/)).toHaveValue('');
  } else expect(api.queryClient.getQueryData(sessionKey)).toEqual(owner);
});

test.each(['forgot-password', 'reset-password'])('validates and retries %s after a rate limit without automatic submissions', async endpoint => {
  const user = userEvent.setup(); let calls = 0;
  server.use(http.post(`http://localhost/api/auth/${endpoint}`, () => { calls++; return failure(429, [], { 'Retry-After': '2' }); }));
  mount(`/${endpoint}${endpoint === 'reset-password' ? `#token=${token}` : ''}`);
  const label = endpoint === 'reset-password' ? 'Reset password' : 'Send reset link';
  await user.click(screen.getByRole('button', { name: label })); expect(calls).toBe(0);
  if (endpoint === 'reset-password') await user.type(screen.getByLabelText(/^New password/), secret);
  else await user.type(screen.getByRole('textbox', { name: 'Email address' }), owner.email);
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  fireEvent.click(screen.getByRole('button', { name: label }));
  await vi.waitFor(() => { expect(screen.getByText(/Try again in/)).toBeVisible(); });
  expect(screen.getByRole('button', { name: label })).toBeDisabled(); expect(calls).toBe(1);
  if (endpoint === 'reset-password') expect(screen.getByLabelText(/^New password/)).toHaveValue('');
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByRole('button', { name: label })).toBeEnabled(); expect(calls).toBe(1);
});

test.each(['forgot-password', 'reset-password'])('associates %s validation safely and allows correcting the input', async endpoint => {
  const user = userEvent.setup(); const field = endpoint === 'reset-password' ? 'password' : 'email';
  server.use(http.post(`http://localhost/api/auth/${endpoint}`, () => failure(422, [{ field, message: '<script>Invalid input</script>' }])));
  mount(`/${endpoint}${endpoint === 'reset-password' ? `#token=${token}` : ''}`);
  const input = field === 'password' ? screen.getByLabelText(/^New password/) : screen.getByRole('textbox', { name: 'Email address' });
  await user.type(input, field === 'password' ? secret : owner.email); await user.keyboard('{Enter}');
  await waitFor(() => { expect(input).toHaveAttribute('aria-invalid', 'true'); }); expect(input).toHaveFocus();
  expect(input).toHaveAccessibleDescription(/<script>Invalid input<\/script>/);
  server.use(http.post(`http://localhost/api/auth/${endpoint}`, () => HttpResponse.json({ status: 'accepted' }, { status: 202 })));
  if (field === 'password') await user.type(input, secret);
  await user.keyboard('{Enter}'); expect(await screen.findByRole('region', { name: 'Recovery request received' })).toBeVisible();
});

test('accepts a later reset link without reusing the previous confirmation', async () => {
  const user = userEvent.setup(); const seen: unknown[] = [];
  server.use(http.post('http://localhost/api/auth/reset-password', async ({ request }) => {
    seen.push(await request.json()); return HttpResponse.json({ status: 'accepted' }, { status: 202 });
  }));
  mount(`/reset-password#token=${token}`); await user.type(screen.getByLabelText(/^New password/), secret); await user.keyboard('{Enter}');
  await screen.findByRole('region', { name: 'Recovery request received' });
  act(() => { window.history.pushState(null, '', `/reset-password#token=${'n'.repeat(43)}`); window.dispatchEvent(new PopStateEvent('popstate')); });
  await user.type(await screen.findByLabelText(/^New password/), secret); await user.keyboard('{Enter}');
  await waitFor(() => { expect(seen).toEqual([{ token, password: secret }, { token: 'n'.repeat(43), password: secret }]); });
});


test.each(['/login?returnTo=/jobs', `/reset-password#token=${token}`])('keeps the keyboard skip link separate from credentials on %s', async path => {
  mount(path); await screen.findByRole('button', { name: path.startsWith('/login') ? 'Sign in' : 'Reset password' });
  await waitFor(() => { expect(window.location.hash).toBe(''); });
  act(() => { window.history.pushState(null, '', window.location.pathname + window.location.search + '#main-content'); window.dispatchEvent(new PopStateEvent('popstate')); });
  expect(screen.queryByText('Google sign-in did not complete')).not.toBeInTheDocument();
  expect(screen.queryByText('Reset link unavailable')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: path.startsWith('/login') ? 'Sign in' : 'Reset password' })).toBeVisible();
});

test('does not consume a newer reset link when an earlier request finishes', async () => {
  const user = userEvent.setup(); let release: () => void = () => undefined;
  const pending = new Promise<void>(resolve => { release = resolve; }); const seen: unknown[] = [];
  server.use(http.post('http://localhost/api/auth/reset-password', async ({ request }) => {
    seen.push(await request.json()); await pending; return HttpResponse.json({ status: 'accepted' }, { status: 202 });
  }));
  mount(`/reset-password#token=${token}`); await user.type(screen.getByLabelText(/^New password/), secret); await user.keyboard('{Enter}');
  act(() => { window.history.pushState(null, '', `/reset-password#token=${'n'.repeat(43)}`); window.dispatchEvent(new PopStateEvent('popstate')); });
  release(); await waitFor(() => { expect(screen.getByRole('button', { name: 'Reset password' })).toBeEnabled(); });
  expect(screen.queryByRole('region', { name: 'Recovery request received' })).not.toBeInTheDocument();
  await user.type(screen.getByLabelText(/^New password/), secret); await user.keyboard('{Enter}');
  await waitFor(() => { expect(seen).toEqual([{ token, password: secret }, { token: 'n'.repeat(43), password: secret }]); });
});


test('continues email sign-in to the saved workspace after a cancelled Google attempt', async () => {
  const user = userEvent.setup(); login(true); sessionStorage.setItem('job-hunter.google-return', '/jobs');
  mount('/login#google=cancelled'); await screen.findByText('Google sign-in was cancelled');
  await fill(user); await user.keyboard('{Enter}');
  expect(await screen.findByText('Jobs tools are not available yet')).toBeVisible();
  expect(window.location.pathname).toBe('/jobs');
});
