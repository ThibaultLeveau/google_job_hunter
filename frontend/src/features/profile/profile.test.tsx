import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from 'vitest';
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
beforeAll(() => {
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true; } },
    close: { configurable: true, value(this: HTMLDialogElement) { this.open = false; } },
  });
});
afterAll(() => {
  Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
  Reflect.deleteProperty(HTMLDialogElement.prototype, 'close');
});
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

test.each(['experience', 'project'] as const)('creates, edits and confirms removal of %s with keyboard focus restoration', async kind => {
  const user = userEvent.setup(); const deferred = gate(); let removals = 0;
  server.use(http.post(path + '/facts', async ({ request }) => {
    const body = await request.json() as components['schemas']['FactWrite'];
    expect(body.expected_revision).toBe(0); expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    expect(body.fact).toEqual({ kind, ...(kind === 'experience' ? { company: 'Acme', title: 'Engineer' } : { name: 'Atlas', url: null }),
      start_date: null, end_date: null, ongoing: null, description: null });
    const created = fact(body.fact); snapshot = { ...snapshot, revision: 1, facts: [created] };
    return HttpResponse.json({ outcome: 'created', revision: 1, fact: created });
  }), http.put(path + '/facts/:id', async ({ request, params }) => {
    const body = await request.json() as components['schemas']['FactWrite'];
    expect(body.expected_revision).toBe(1); expect(body.fact).toMatchObject({ start_date: '2020-01-01', end_date: '2022-03-04', ongoing: false, description: 'Manual description' });
    const updated = fact(body.fact, { id: String(params.id) }); snapshot = { ...snapshot, revision: 2, facts: [updated] };
    return HttpResponse.json({ outcome: 'updated', revision: 2, fact: updated });
  }), http.delete(path + '/facts/:id', async ({ request }) => {
    removals++; expect(new URL(request.url).searchParams.get('expected_revision')).toBe('2');
    await deferred.pending; snapshot = { ...snapshot, revision: 3, facts: [] };
    return HttpResponse.json({ outcome: 'deleted', revision: 3, fact: null });
  }));
  mount(); const add = await screen.findByRole('button', { name: `Add ${kind}` });
  add.focus(); await user.keyboard('{Enter}');
  const dialog = await screen.findByRole('dialog', { name: `Add ${kind}` });
  const name = within(dialog).getByLabelText(kind === 'experience' ? 'Company' : 'Project name', { exact: false });
  expect(name).toHaveFocus(); await user.type(name, kind === 'experience' ? 'Acme' : 'Atlas');
  if (kind === 'experience') await user.type(within(dialog).getByLabelText(/Job title/), 'Engineer');
  await user.type(within(dialog).getByLabelText('Description (optional)'), '   ');
  await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  await waitFor(() => { expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(add).toHaveFocus(); });
  const edit = screen.getByRole('button', { name: `Edit ${kind}` }); await user.click(edit);
  const editing = await screen.findByRole('dialog', { name: `Edit ${kind}` });
  fireEvent.change(within(editing).getByLabelText('Start date (optional)'), { target: { value: '2020-01-01' } });
  fireEvent.change(within(editing).getByLabelText('End date (optional)'), { target: { value: '2022-03-04' } });
  await user.selectOptions(within(editing).getByRole('combobox', { name: 'Ongoing?' }), 'no');
  await user.clear(within(editing).getByLabelText('Description (optional)')); await user.type(within(editing).getByLabelText('Description (optional)'), 'Manual description');
  await user.click(within(editing).getByRole('button', { name: 'Save changes' }));
  await waitFor(() => { expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(edit).toHaveFocus(); });
  const remove = screen.getByRole('button', { name: `Remove ${kind}` }); await user.click(remove);
  expect(await screen.findByRole('dialog', { name: `Remove ${kind}` })).toHaveTextContent('source document is kept');
  expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true })); expect(remove).toHaveFocus(); expect(removals).toBe(0);
  await user.keyboard('{Enter}'); await user.tab(); expect(screen.getByRole('button', { name: 'Confirm removal' })).toHaveFocus();
  await user.keyboard('{Enter}'); expect(await screen.findByRole('button', { name: 'Removing…' })).toBeDisabled();
  fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true })); expect(screen.getByRole('dialog')).toBeVisible();
  deferred.release(); await waitFor(() => { expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(add).toHaveFocus(); }); expect(removals).toBe(1);
});

test('associates required/date errors and rejects reversed dates or an ongoing end date', async () => {
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Add experience' }));
  const dialog = screen.getByRole('dialog'); const save = within(dialog).getByRole('button', { name: 'Save changes' });
  await user.click(save); expect(within(dialog).getByLabelText(/Company/)).toHaveAccessibleDescription('Error: Enter 1–200 characters.');
  expect(within(dialog).getByLabelText(/Company/)).toHaveFocus();
  await user.type(within(dialog).getByLabelText(/Company/), 'Acme'); await user.type(within(dialog).getByLabelText(/Job title/), 'Engineer');
  fireEvent.change(within(dialog).getByLabelText('Start date (optional)'), { target: { value: '2025-01-02' } });
  const end = within(dialog).getByLabelText('End date (optional)'); fireEvent.change(end, { target: { value: '2025-01-01' } });
  await user.click(save); expect(end).toHaveAccessibleDescription(/on or after start date/); expect(end).toHaveFocus();
  fireEvent.change(end, { target: { value: '2025-01-03' } }); await user.selectOptions(within(dialog).getByRole('combobox'), 'yes');
  await user.click(save); expect(end).toHaveAccessibleDescription(/Clear the end date/);
  fireEvent.change(end, { target: { value: '' } });
  server.use(http.post(path + '/facts', async ({ request }) => {
    expect(await request.json()).toMatchObject({ fact: { ongoing: true, end_date: null } });
    return HttpResponse.json({ outcome: 'created', revision: 1, fact: null });
  }));
  await user.click(save); await waitFor(() => { expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); });
});

test.each(['javascript:alert(1)', 'https://user:password@example.org', 'not a URL'])('rejects unsafe project URL %s without sending a request', async url => {
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Add project' }));
  const dialog = screen.getByRole('dialog'); await user.type(within(dialog).getByLabelText(/Project name/), 'Atlas');
  const link = within(dialog).getByLabelText('Project link (optional)'); await user.type(link, url);
  await user.click(within(dialog).getByRole('button', { name: 'Save changes' })); expect(link).toHaveAccessibleDescription(/HTTP or HTTPS/); expect(link).toHaveFocus();
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' })); expect(screen.getByRole('button', { name: 'Add project' })).toHaveFocus();
});

test('keeps edited values through conflict and failed reload, then saves against the explicitly refreshed revision', async () => {
  const original = fact({ kind: 'project', name: 'Atlas', url: 'https://example.org', ongoing: false }, {
    source_associations: [{ document_id: documentId, source_version: 1, language: 'en', locator: 'page:1', quote: 'Atlas' }],
  }); snapshot = { ...snapshot, revision: 1, facts: [original] }; let saves = 0;
  server.use(http.put(path + '/facts/:id', async ({ request }) => {
    saves++; const body = await request.json() as components['schemas']['FactWrite'];
    if (saves === 1) { expect(body.expected_revision).toBe(1); snapshot.revision = 3; return failure(409); }
    expect(body).toMatchObject({ expected_revision: 3, fact: { kind: 'project', name: 'My unsaved title', url: 'https://example.org' } });
    snapshot = { ...snapshot, revision: 4, facts: [fact(body.fact, { id: original.id, user_fields: ['name'] })] };
    return HttpResponse.json({ outcome: 'updated', revision: 4, fact: snapshot.facts?.[0] });
  }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Edit project' }));
  const dialog = screen.getByRole('dialog'); const name = within(dialog).getByLabelText(/Project name/);
  expect(name).toHaveValue('Atlas'); expect(within(dialog).getByText('Atlas', { selector: 'blockquote' })).toBeInTheDocument();
  await user.clear(name); await user.type(name, 'My unsaved title'); await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent(/profile changed/); expect(name).toHaveValue('My unsaved title');
  expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeDisabled();
  server.use(http.get(path, () => failure(503))); await user.click(within(dialog).getByRole('button', { name: 'Reload latest facts' }));
  expect(await within(dialog).findByText(/latest profile could not be loaded/)).toBeVisible(); expect(name).toHaveValue('My unsaved title');
  server.use(http.get(path, () => HttpResponse.json(snapshot))); await user.click(within(dialog).getByRole('button', { name: 'Reload latest facts' }));
  expect(await within(dialog).findByText(/Latest facts loaded/)).toBeVisible(); expect(name).toHaveValue('My unsaved title'); expect(saves).toBe(1);
  await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  expect(await screen.findByRole('heading', { name: 'My unsaved title' })).toBeVisible();
  await waitFor(() => { expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); }); expect(saves).toBe(2);
});

test('shows server duplicate results and retains the saved fact without browser deduplication', async () => {
  const original = fact({ kind: 'project', name: 'Atlas' }); snapshot.facts = [original]; let calls = 0;
  server.use(http.post(path + '/facts', async ({ request }) => {
    calls++; expect(await request.json()).toMatchObject({ fact: { name: '  ATLAS  ' } });
    return HttpResponse.json({ outcome: 'existing', revision: 0, fact: original });
  }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Add project' }));
  await user.type(screen.getByLabelText(/Project name/), '  ATLAS  '); await user.click(screen.getByRole('button', { name: 'Save changes' }));
  expect(await screen.findByText('This fact already exists. The saved values were kept.')).toBeVisible();
  await waitFor(() => { expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); });
  expect(screen.getAllByRole('heading', { name: 'Atlas' })).toHaveLength(1); expect(calls).toBe(1);
});

test.each([422, 503])('retains project values after a %i failure and prevents repeated saves while pending', async status => {
  const deferred = gate(); let calls = 0;
  server.use(http.post(path + '/facts', async () => { calls++; await deferred.pending; return failure(status); }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Add project' }));
  const dialog = screen.getByRole('dialog'); const name = within(dialog).getByLabelText(/Project name/); await user.type(name, 'Unsaved');
  await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  expect(await within(dialog).findByRole('button', { name: 'Saving…' })).toBeDisabled(); fireEvent(dialog, new Event('cancel', { cancelable: true }));
  const form = dialog.querySelector('form'); if (!form) throw new Error('Missing editor form'); fireEvent.submit(form); expect(calls).toBe(1);
  deferred.release(); expect(await within(dialog).findByRole('alert')).not.toHaveTextContent('Private server message'); expect(name).toHaveValue('Unsaved');
});

test('retains a removed fact draft but prevents saving after conflict reload confirms it is gone', async () => {
  const original = fact({ kind: 'project', name: 'Atlas' }); snapshot.facts = [original];
  server.use(http.put(path + '/facts/:id', () => { snapshot = { ...snapshot, revision: 2, facts: [] }; return failure(404); }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Edit project' }));
  const dialog = screen.getByRole('dialog'); await user.type(within(dialog).getByLabelText(/Project name/), ' draft');
  await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  await user.click(await within(dialog).findByRole('button', { name: 'Reload latest facts' }));
  expect(await within(dialog).findByText(/This fact was removed elsewhere/)).toBeVisible();
  expect(within(dialog).getByLabelText(/Project name/)).toHaveValue('Atlas draft'); expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeDisabled();
});

test('closes the private editor and clears its draft when the session expires', async () => {
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Add project' }));
  await user.type(screen.getByLabelText(/Project name/), 'Private editor draft'); current = null;
  server.use(http.post(path + '/facts', () => failure(401))); await user.click(screen.getByRole('button', { name: 'Save changes' }));
  expect(await screen.findByText(/Your session has expired/)).toBeVisible(); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(api.queryClient.getQueryData(['profile', owner.id])).toBeUndefined();
});

test('wraps Tab and Shift+Tab within the editor', async () => {
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Add project' }));
  const dialog = screen.getByRole('dialog'); const first = within(dialog).getByLabelText(/Project name/);
  const last = within(dialog).getByRole('button', { name: 'Save changes' });
  expect(first).toHaveFocus(); await user.tab({ shift: true }); expect(last).toHaveFocus();
  await user.tab(); expect(first).toHaveFocus();
});
