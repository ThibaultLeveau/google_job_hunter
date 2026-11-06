import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test } from 'vitest';
import type { components } from '../../../generated/api';
import App from '../../App';
import { api, sessionKey, type Session } from '../../shared/api';
import { server } from '../../test/setup';

type Snapshot = components['schemas']['ProfileSnapshot'];
type Fact = components['schemas']['ProfileFact'];
const owner: Session = { id: '00000000-0000-4000-8000-000000000001', email: 'owner@example.org', email_verified: true, csrf_token: 'c'.repeat(64) };
const documentId = '00000000-0000-4000-8000-000000000002';
const path = 'http://localhost/api/v1/profile';
let snapshot: Snapshot;
let current: Session | null;
function fact(data: Fact['data'], fields: Partial<Fact> = {}): Fact {
  return { id: crypto.randomUUID(), data, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', ...fields };
}
const failure = (status: number) => HttpResponse.json({ code: 'fixture', message: 'Private server message', field_errors: [], request_id: documentId }, { status });
function gate() { let release: () => void = () => undefined; const pending = new Promise<void>(resolve => { release = resolve; }); return { pending, release }; }
function mount() {
  window.history.replaceState(null, '', '/profile'); api.acceptSession(current);
  return render(<QueryClientProvider client={api.queryClient}><App /></QueryClientProvider>);
}
beforeEach(() => {
  api.clearSession(); api.queryClient.clear(); current = owner; snapshot = { id: owner.id, revision: 0, facts: [] };
  api.queryClient.setQueryDefaults(sessionKey, { retry: false });
  api.queryClient.setQueryDefaults(['profile'], { retry: false });
  server.use(http.get('http://localhost/api/auth/me', () => current ? HttpResponse.json(current) : failure(401)),
    http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: false })),
    http.get(path, () => HttpResponse.json(snapshot)));
});
afterEach(() => { api.clearSession(); api.queryClient.clear(); });

test('renders loading, honest empty sections and evaluation refresh guidance', async () => {
  const deferred = gate(); server.use(http.get(path, async () => { await deferred.pending; return HttpResponse.json(snapshot); }));
  mount(); expect(await screen.findByText('Loading your profile')).toBeVisible();
  expect(screen.queryByRole('textbox', { name: 'Skill name' })).not.toBeInTheDocument(); deferred.release();
  expect(await screen.findByText(/No skills yet/)).toBeVisible();
  expect(screen.getByText('No accepted experience yet.')).toBeVisible();
  expect(screen.getByText('No accepted projects yet.')).toBeVisible();
  expect(screen.getByText(/Changing them will require evaluations to refresh/)).toBeVisible();
});

test('renders accepted facts, literal hostile text, unknown values and safe source links', async () => {
  const hostile = '<img src=x onerror=alert(1)>';
  snapshot.facts = [fact({ kind: 'skill', name: hostile }),
    fact({ kind: 'experience', company: 'Acme', title: 'Engineer', ongoing: true, start_date: '2020-01-01', description: hostile }, {
      user_fields: ['title'], source_associations: [{ document_id: documentId, source_version: 1, language: 'fr', locator: 'paragraph:1', quote: hostile }],
    }), fact({ kind: 'project', name: 'Atlas', end_date: '2025-01-01', url: 'https://example.org/Atlas' }, { unavailable_sources: [documentId] }),
    fact({ kind: 'project', name: 'Unlinked' }), fact({ kind: 'project', name: 'Unsafe', url: 'javascript:alert(1)' }),
    fact({ kind: 'experience', company: 'Elsewhere', title: 'Writer', ongoing: false }, {
      source_associations: [{ document_id: documentId, source_version: 2, language: null, locator: 'page:1', quote: 'Writer' }],
    })];
  const user = userEvent.setup(); mount();
  expect(await screen.findByRole('heading', { name: hostile })).toBeVisible();
  expect(screen.getByText('Source unavailable')).toBeVisible(); expect(screen.getByText('Includes manual edits')).toBeVisible();
  expect(screen.getAllByText('Source-supported')).toHaveLength(2);
  expect(screen.getAllByText('Manual')).toHaveLength(3);
  expect(screen.getByText('2020-01-01 — Present')).toBeVisible();
  expect(screen.getAllByText(/Start date unknown/).length).toBeGreaterThan(0);
  expect(screen.getByRole('link', { name: 'https://example.org/Atlas' })).toHaveAttribute('href', 'https://example.org/Atlas');
  expect(screen.queryByRole('link', { name: 'javascript:alert(1)' })).not.toBeInTheDocument();
  const disclosure = screen.getAllByText('View source evidence (1)')[0];
  if (!disclosure) throw new Error('Missing evidence disclosure');
  disclosure.focus(); await user.keyboard('{Enter}'); const details = disclosure.closest('details');
  if (!details) throw new Error('Missing evidence details');
  expect(within(details).getByRole('link')).toHaveAttribute('href', `/api/v1/documents/${documentId}/download`);
  expect(screen.getByText('page:1 · Language: Unknown')).toBeInTheDocument();
  expect(document.querySelector('img')).toBeNull();
});

test('adds and removes skills by keyboard using revisions, CSRF and fresh queries', async () => {
  const user = userEvent.setup(); const created = fact({ kind: 'skill', name: 'Python' }); let reads = 0;
  server.use(http.get(path, () => { reads++; return HttpResponse.json(snapshot); }), http.post(path + '/facts', async ({ request }) => {
    expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    expect(await request.json()).toEqual({ expected_revision: 0, fact: { kind: 'skill', name: 'Python' } });
    snapshot = { ...snapshot, revision: 1, facts: [created] }; return HttpResponse.json({ outcome: 'created', revision: 1, fact: created }, { status: 201 });
  }), http.delete(path + '/facts/:id', ({ request, params }) => {
    expect(params.id).toBe(created.id); expect(new URL(request.url).searchParams.get('expected_revision')).toBe('1');
    expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    snapshot = { ...snapshot, revision: 2, facts: [] }; return HttpResponse.json({ outcome: 'deleted', revision: 2, fact: null });
  }));
  mount(); const input = await screen.findByRole('textbox', { name: 'Skill name' }); input.focus();
  await user.keyboard('Python{Enter}'); expect(await screen.findByRole('heading', { name: 'Python' })).toBeVisible();
  await waitFor(() => { expect(input).toHaveFocus(); }); expect(input).toHaveValue('');
  await user.tab(); expect(screen.getByRole('button', { name: 'Add skill' })).toHaveFocus();
  await user.tab(); expect(screen.getByRole('button', { name: 'Remove Python' })).toHaveFocus(); await user.keyboard('{Enter}');
  expect(await screen.findByText('Skill removed.')).toBeVisible();
  await waitFor(() => { expect(screen.queryByRole('heading', { name: 'Python' })).not.toBeInTheDocument(); expect(input).toHaveFocus(); });
  expect(reads).toBe(3);
});

test('shows the server normalized-duplicate outcome without replacing original display text', async () => {
  const created = fact({ kind: 'skill', name: 'Python' }); snapshot = { ...snapshot, revision: 1, facts: [created] };
  server.use(http.post(path + '/facts', async ({ request }) => {
    expect(await request.json()).toEqual({ expected_revision: 1, fact: { kind: 'skill', name: '  ＰＹＴＨＯＮ  ' } });
    return HttpResponse.json({ outcome: 'existing', revision: 1, fact: created });
  }));
  const user = userEvent.setup(); mount(); await user.type(await screen.findByRole('textbox', { name: 'Skill name' }), '  ＰＹＴＨＯＮ  '); await user.keyboard('{Enter}');
  expect(await screen.findByText(/That skill is already/)).toBeVisible();
  expect(screen.getAllByRole('heading', { name: 'Python' })).toHaveLength(1);
});

test.each([409, 404])('keeps the draft after %i until explicit reload and review, with no automatic retry', async status => {
  let calls = 0; server.use(http.post(path + '/facts', async ({ request }) => {
    calls++; const body = await request.json();
    if (calls === 1) { snapshot.revision = 4; return failure(status); }
    expect(body).toEqual({ expected_revision: 4, fact: { kind: 'skill', name: 'Rust' } });
    return HttpResponse.json({ outcome: 'created', revision: 5, fact: fact({ kind: 'skill', name: 'Rust' }) });
  }));
  const user = userEvent.setup(); mount(); const input = await screen.findByRole('textbox', { name: 'Skill name' });
  await user.type(input, 'Rust{Enter}'); expect(await screen.findByText('Your profile changed elsewhere')).toBeVisible();
  expect(input).toHaveValue('Rust'); expect(screen.getByRole('button', { name: 'Add skill' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Reload profile' }));
  expect(await screen.findByText(/Profile reloaded/)).toBeVisible(); expect(input).toHaveValue('Rust'); expect(calls).toBe(1);
  await user.keyboard('{Enter}'); expect(await screen.findByText('Skill added.')).toBeVisible(); expect(calls).toBe(2);
});

test('blocks duplicate submissions while saving and preserves the submitted name', async () => {
  const deferred = gate(); let calls = 0;
  server.use(http.post(path + '/facts', async () => { calls++; await deferred.pending; return HttpResponse.json({ outcome: 'created', revision: 1, fact: null }); }));
  const user = userEvent.setup(); mount(); const input = await screen.findByRole('textbox', { name: 'Skill name' });
  await user.type(input, 'Go{Enter}'); const adding = await screen.findByRole('button', { name: 'Adding skill…' });
  expect(adding).toBeDisabled(); expect(input).toHaveAttribute('readonly');
  const form = input.closest('form'); if (!form) throw new Error('Missing skill form');
  fireEvent.submit(form); await user.keyboard('{Enter}'); expect(calls).toBe(1);
  deferred.release(); expect(await screen.findByText('Skill added.')).toBeVisible();
});

test('validates blank input and associates errors with the field', async () => {
  const user = userEvent.setup(); mount(); const input = await screen.findByRole('textbox', { name: 'Skill name' });
  await user.type(input, '   {Enter}'); expect(await screen.findByRole('alert')).toHaveTextContent('1–120 characters');
  expect(input).toHaveAttribute('aria-invalid', 'true'); expect(input).toHaveAccessibleDescription(/1–120 characters/); expect(input).toHaveFocus();
  await user.type(input, 'R'); expect(input).not.toHaveAttribute('aria-invalid', 'true');
});

test.each([422, 503])('preserves the draft and gives safe recovery for mutation %i', async status => {
  server.use(http.post(path + '/facts', () => failure(status)));
  const user = userEvent.setup(); mount(); const input = await screen.findByRole('textbox', { name: 'Skill name' });
  await user.type(input, 'SQL{Enter}'); expect(await screen.findByRole('alert')).not.toHaveTextContent('Private server message'); expect(input).toHaveValue('SQL');
  if (status === 422) expect(input).toHaveAttribute('aria-invalid', 'true');
  else { await user.click(screen.getByRole('button', { name: 'Reload profile' })); expect(await screen.findByText(/Profile reloaded/)).toBeVisible(); }
});

test('retries an initial load and preserves a draft when a later reload fails', async () => {
  server.use(http.get(path, () => failure(503))); const user = userEvent.setup(); mount();
  expect(await screen.findByText('Your profile could not be loaded')).toBeVisible();
  server.use(http.get(path, () => HttpResponse.json(snapshot))); await user.click(screen.getByRole('button', { name: 'Try again' }));
  const input = await screen.findByRole('textbox', { name: 'Skill name' }); await user.type(input, 'Draft');
  server.use(http.get(path, () => failure(503)));
  await act(async () => { await api.queryClient.invalidateQueries({ queryKey: ['profile'] }); });
  expect(await screen.findByText('This profile may be out of date')).toBeVisible(); expect(input).toHaveValue('Draft');
  await user.click(screen.getByRole('button', { name: 'Reload profile' }));
  expect(screen.getByRole('button', { name: 'Add skill' })).toBeDisabled();
  server.use(http.get(path, () => HttpResponse.json(snapshot))); await user.click(screen.getByRole('button', { name: 'Reload profile' }));
  await waitFor(() => { expect(screen.getByRole('button', { name: 'Add skill' })).toBeEnabled(); }); expect(input).toHaveValue('Draft');
});

test('refreshes an expired session and clears private profile data', async () => {
  snapshot.facts = [fact({ kind: 'skill', name: 'Private skill' })]; const user = userEvent.setup(); mount();
  const input = await screen.findByRole('textbox', { name: 'Skill name' }); await user.type(input, 'Draft');
  current = null; server.use(http.post(path + '/facts', () => failure(401)));
  await user.keyboard('{Enter}'); expect(await screen.findByText(/Your session has expired/)).toBeVisible();
  expect(api.queryClient.getQueryData(['profile', owner.id])).toBeUndefined(); expect(screen.queryByText('Private skill')).not.toBeInTheDocument();
});

test('clears cached facts and drafts on account change and ignores late mutation results', async () => {
  snapshot.facts = [fact({ kind: 'skill', name: 'First account secret' })]; const deferred = gate();
  server.use(http.post(path + '/facts', async () => { await deferred.pending; return HttpResponse.json({ outcome: 'created', revision: 99, fact: null }); }));
  const user = userEvent.setup(); mount(); const input = await screen.findByRole('textbox', { name: 'Skill name' });
  await user.type(input, 'First account draft{Enter}'); await screen.findByRole('button', { name: 'Adding skill…' });
  current = { ...owner, id: documentId, email: 'second@example.org', csrf_token: 'd'.repeat(64) }; snapshot = { id: current.id, revision: 0, facts: [] };
  act(() => { api.acceptSession(current); });
  expect(await screen.findByText(/No skills yet/)).toBeVisible(); deferred.release();
  await waitFor(() => { expect(screen.getByRole('textbox', { name: 'Skill name' })).toHaveValue(''); });
  expect(api.queryClient.getQueryData(['profile', owner.id])).toBeUndefined();
  expect(api.queryClient.getMutationCache().getAll()).toHaveLength(0);
  expect(screen.queryByText('First account secret')).not.toBeInTheDocument(); expect(screen.queryByText('Skill added.')).not.toBeInTheDocument();
});
