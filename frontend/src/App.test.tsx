import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import App from './App';
import { AppErrorBoundary } from './app/Shell';
import { authRoutes, safeReturnPath, sessionNotice, workspaceRoutes } from './app/routes';
import { api, sessionKey, type Session } from './shared/api';
import { server } from './test/setup';

const owner: Session = { id: 'f8eabf03-4823-47ec-9b28-fe03a2cc33b6',
  email: 'researcher@example.org', email_verified: true, csrf_token: 'c'.repeat(64) };
const denied = () => HttpResponse.json({ code: 'unauthenticated', message: 'Sign in.',
  field_errors: [], request_id: 'bcb2f310-f4a5-4a2d-9871-a220704e5da8' }, { status: 401 });

beforeEach(() => {
  server.use(http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: false })));
  api.queryClient.setQueryDefaults(['auth-providers'], { retry: false });
  api.clearSession(); api.queryClient.clear();
  api.queryClient.setQueryDefaults(sessionKey, { retry: false });
});
afterEach(() => { api.clearSession(); api.queryClient.clear(); api.queryClient.setQueryDefaults(sessionKey, {}); });
function mount(path: string, authenticated = false) {
  window.history.replaceState(null, '', path);
  if (authenticated) api.acceptSession(owner);
  return render(<QueryClientProvider client={api.queryClient}><App /></QueryClientProvider>);
}
function sessionResponse(value: Session | null = owner) {
  server.use(http.get('http://localhost/api/auth/me', () => value ? HttpResponse.json(value) : denied()));
}

test('withholds protected content until the initial session check completes', async () => {
  let release: () => void = () => undefined;
  const pending = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get('http://localhost/api/auth/me', async () => { await pending; return HttpResponse.json(owner); }));
  mount('/profile');
  expect(screen.getByRole('status')).toHaveTextContent('Checking your session');
  expect(screen.queryByText('Profile tools are not available yet')).not.toBeInTheDocument();
  expect(screen.queryByRole('navigation', { name: 'Workspace' })).not.toBeInTheDocument();
  release();
  expect(await screen.findByText('Profile tools are not available yet')).toBeVisible();
});

test.each(workspaceRoutes)('guards $path and retains its internal query and fragment', async route => {
  sessionResponse(null);
  mount(`${route.path}?sort=new#saved`);
  expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible();
  expect(window.location.pathname).toBe('/login');
  expect(new URLSearchParams(window.location.search).get('returnTo')).toBe(`${route.path}?sort=new#saved`);
  expect(screen.queryByText(`${route.label} tools are not available yet`)).not.toBeInTheDocument();
  expect(screen.queryByText(/session has expired/)).not.toBeInTheDocument();
});

test.each(workspaceRoutes)('opens $path for an authenticated session with active navigation and title', async route => {
  sessionResponse(); mount(route.path);
  const navigation = await screen.findByRole('navigation', { name: 'Workspace' });
  expect(within(navigation).getByRole('link', { name: route.label })).toHaveAttribute('aria-current', 'page');
  expect(document.title).toBe(`${route.title} · Job Hunter`);
  expect(screen.getByRole('main')).toHaveAttribute('tabindex', '-1');
});

test('uses the router’s matching rules for trailing slashes and case in page titles', async () => {
  sessionResponse(); mount('/PROFILE/');
  expect(await screen.findByRole('navigation', { name: 'Workspace' })).toBeVisible();
  expect(screen.getByRole('heading', { level: 1, name: 'Your profile' })).toBeVisible();
  expect(document.title).toBe('Your profile · Job Hunter');
});

test.each(authRoutes)('allows public access to $path', async route => {
  sessionResponse(null); mount(route.path);
  expect(await screen.findByRole('heading', { level: 1, name: route.title })).toBeVisible();
  expect(window.location.pathname).toBe(route.path);
});

test.each([null, '', 'https://other.example/jobs', '//other.example/jobs', '//job-hunter.invalid/jobs',
  'javascript:alert(1)', '/\\other.example/jobs', '/jobs/%5csecret', '/jobs/%00', '/jobs/%252f',
  '/jobs/%', '/jobs/../login', '/login', '/jobs-other', '/jobs\n', '/jobs/' + 'x'.repeat(2048)])(
  'rejects an unsafe or unsupported return path: %s', value => { expect(safeReturnPath(value)).toBe('/profile'); },
);
test.each(['/profile', '/searches?q=engineer', '/jobs/42?sort=new#evidence', '/settings'])('keeps a safe return path: %s', path => {
  expect(safeReturnPath(path)).toBe(path);
});

test.each([['/login?returnTo=%2Fjobs%3Fsort%3Dnew', '/jobs'],
  ['/register?returnTo=https%3A%2F%2Fother.example', '/profile'], ['/LOGIN/?returnTo=/jobs', '/jobs'], ['/', '/profile']])(
  'routes an already authenticated visitor from %s to %s', async (from, target) => {
    sessionResponse(); mount(from, true);
    await waitFor(() => { expect(window.location.pathname).toBe(target); });
    expect(await screen.findByRole('navigation', { name: 'Workspace' })).toBeVisible();
  },
);

test('supports the skip link, account disclosure, Escape, blur, route focus and keyboard navigation', async () => {
  const user = userEvent.setup(); sessionResponse(); mount('/profile', true);
  const skip = screen.getByRole('link', { name: 'Skip to main content' });
  await user.tab(); expect(skip).toHaveFocus();
  expect(skip).toHaveAttribute('href', '#main-content');
  await user.tab(); expect(screen.getByRole('link', { name: /Job Hunter/ })).toHaveFocus();
  await user.tab(); const account = screen.getByRole('button', { name: 'Account' }); expect(account).toHaveFocus();
  await user.keyboard('{Enter}'); expect(account).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByText(owner.email)).toBeVisible();
  await user.tab(); expect(screen.getByRole('link', { name: 'Account settings' })).toHaveFocus();
  await user.keyboard('{Escape}'); expect(account).toHaveFocus(); expect(account).toHaveAttribute('aria-expanded', 'false');
  await user.keyboard(' '); await user.tab(); await user.tab();
  expect(screen.getByRole('button', { name: 'Sign out' })).toHaveFocus();
  await user.tab(); expect(account).toHaveAttribute('aria-expanded', 'false');
  await user.click(screen.getByRole('link', { name: 'Jobs' }));
  await waitFor(() => { expect(screen.getByRole('main')).toHaveFocus(); });
  expect(document.title).toBe('Your job research · Job Hunter');
  await user.click(screen.getByRole('button', { name: 'Account' }));
  await user.click(screen.getByRole('link', { name: 'Account settings' }));
  expect(await screen.findByRole('heading', { name: 'Account settings', level: 1 })).toBeVisible();
  expect(screen.getByRole('button', { name: 'Account' })).toHaveAttribute('aria-expanded', 'false');
});

test('redirects expired sessions and clears private data after a session refresh', async () => {
  sessionResponse(); mount('/jobs?sort=new', true);
  expect(await screen.findByRole('navigation', { name: 'Workspace' })).toBeVisible();
  api.queryClient.setQueryData(['private'], 'private fixture');
  sessionResponse(null);
  await act(async () => { await api.invalidateSession(); });
  expect(await screen.findByText('Your session has expired. Sign in again to continue.')).toBeVisible();
  expect(api.queryClient.getQueryData(['private'])).toBeUndefined();
  expect(screen.queryByRole('navigation', { name: 'Workspace' })).not.toBeInTheDocument();
  expect(new URLSearchParams(window.location.search).get('returnTo')).toBe('/jobs?sort=new');
});

test('offers a retry when a session check fails instead of showing protected content', async () => {
  server.use(http.get('http://localhost/api/auth/me', () => HttpResponse.json({}, { status: 503 })));
  const user = userEvent.setup(); mount('/profile');
  expect(await screen.findByRole('alert')).toHaveTextContent('We couldn’t check your session');
  expect(screen.queryByText('Profile tools are not available yet')).not.toBeInTheDocument();
  sessionResponse(); await user.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByText('Profile tools are not available yet')).toBeVisible();
});

test.each([204, 503])('sign out clears private caches and reports the actual %i outcome', async status => {
  const user = userEvent.setup(); sessionResponse(); mount('/profile', true);
  let calls = 0; let release: () => void = () => undefined;
  const pending = new Promise<void>(resolve => { release = resolve; });
  server.use(http.post('http://localhost/api/auth/logout', async ({ request }) => {
    expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token); calls++;
    await pending; return new HttpResponse(null, { status });
  }));
  api.queryClient.setQueryData(['private'], 'private fixture');
  await user.click(screen.getByRole('button', { name: 'Account' }));
  await user.click(screen.getByRole('button', { name: 'Sign out' }));
  const signingOut = await screen.findByRole('button', { name: 'Signing out…' });
  expect(signingOut).toBeDisabled(); await user.click(signingOut);
  release();
  expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible();
  expect(api.queryClient.getQueryData(['private'])).toBeUndefined();
  expect(calls).toBe(1);
  if (status === 204) {
    expect(await screen.findByText('You are signed out.')).toBeVisible();
    act(() => { api.acceptSession(owner); });
    expect(await screen.findByText('Profile tools are not available yet')).toBeVisible();
    sessionResponse(null); await act(async () => { await api.invalidateSession(); });
    expect(await screen.findByText('Your session has expired. Sign in again to continue.')).toBeVisible();
  } else {
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign out could not be confirmed');
    expect(screen.getByRole('link', { name: 'Check session again' })).toHaveAttribute('href', '/login');
    expect(screen.queryByText('You are signed out.')).not.toBeInTheDocument();
  }
});

test('renders a useful not-found state and returns through real navigation', async () => {
  sessionResponse(); const user = userEvent.setup(); mount('/missing', true);
  expect(screen.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible();
  await user.click(screen.getByRole('link', { name: 'Return to workspace' }));
  expect(await screen.findByRole('heading', { level: 1, name: 'Your profile' })).toBeVisible();
});

test('contains unexpected render errors without displaying their private details', () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  function Broken(): React.ReactNode { throw new Error('private-error-fixture'); }
  render(<AppErrorBoundary><Broken /></AppErrorBoundary>);
  expect(screen.getByRole('alert')).toHaveTextContent('This page could not be displayed');
  expect(screen.queryByText(/private-error-fixture/)).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Reload workspace' })).toHaveAttribute('href', '/');
  expect(screen.getByRole('main')).toHaveFocus();
  expect(document.title).toBe('Something went wrong · Job Hunter');
});

test.each([null, 'expired', {}, { reason: '<img src=x>' }])('ignores unrecognized history state: %j', value => {
  expect(sessionNotice(value)).toBeUndefined();
});
