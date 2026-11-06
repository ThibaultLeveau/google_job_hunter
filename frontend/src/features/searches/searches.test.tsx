import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test } from 'vitest';
import type { components } from '../../../generated/api';
import App from '../../App';
import { api, sessionKey, type Session } from '../../shared/api';
import { server } from '../../test/setup';
import type { SavedSearch } from './SearchEditor';

const owner: Session = { id: '00000000-0000-4000-8000-000000000001', email: 'owner@example.org', email_verified: true, csrf_token: 'c'.repeat(64) };
const path = 'http://localhost/api/v1/searches';
const failure = (status: number) => HttpResponse.json({ code: 'fixture', message: 'Private server message', field_errors: [], request_id: owner.id }, { status });
let current: Session | null, items: SavedSearch[];
function search(definition: SavedSearch['definition'] = { groups: [['Python']] }): SavedSearch {
  return { id: crypto.randomUUID(), name: 'Engineering', revision: 1, definition, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' };
}
function gate() { let release: () => void = () => undefined; const pending = new Promise<void>(resolve => { release = resolve; }); return { pending, release }; }
function mount() {
  window.history.replaceState(null, '', '/searches'); api.acceptSession(current);
  return render(<QueryClientProvider client={api.queryClient}><App /></QueryClientProvider>);
}
const button = (name: string) => screen.getByRole('button', { name });
const field = (name: string) => screen.getByRole('textbox', { name });
async function edit() { const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Edit Engineering' })); return user; }
beforeEach(() => {
  api.clearSession(); api.queryClient.clear(); current = owner; items = [];
  api.queryClient.setQueryDefaults(sessionKey, { retry: false }); api.queryClient.setQueryDefaults(['searches'], { retry: false });
  server.use(http.get('http://localhost/api/auth/me', () => current ? HttpResponse.json(current) : failure(401)),
    http.get('http://localhost/api/auth/providers', () => HttpResponse.json({ google: false })),
    http.get(path, () => HttpResponse.json({ items, next_offset: null })),
    http.get(path + '/:id', ({ params }) => { const value = items.find(item => item.id === params.id); return value ? HttpResponse.json(value) : failure(404); }));
});
afterEach(() => { api.clearSession(); api.queryClient.clear(); });

test('shows loading and empty states, with keyboard entry, labels and focus recovery', async () => {
  const deferred = gate(); server.use(http.get(path, async () => { await deferred.pending; return HttpResponse.json({ items, next_offset: null }); }));
  const user = userEvent.setup(); mount(); expect(await screen.findByText('Loading saved searches')).toBeVisible();
  expect(button('New search')).toBeDisabled(); deferred.release(); expect(await screen.findByText('No saved searches yet')).toBeVisible();
  button('New search').focus(); await user.keyboard('{Enter}'); expect(field('Search name')).toHaveFocus();
  await user.tab(); expect(field('Group 1 term 1')).toHaveFocus();
  expect(button('Remove term 1 from group 1')).toBeDisabled(); expect(button('Remove group 1')).toBeDisabled();
  expect(screen.getByText(/separate from filters on verified job details/)).toBeVisible();
  await user.click(button('Cancel editing')); expect(button('New search')).toHaveFocus();
});

test('creates AND-of-OR phrases and exclusions, round trips duplicates, updates a revision and deletes', async () => {
  const user = userEvent.setup(); let writes = 0;
  server.use(http.post(path, async ({ request }) => {
    writes++; expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    const body = await request.json() as components['schemas']['SearchWrite'];
    expect(body).toEqual({ name: 'Backend roles', definition: { groups: [[' Ｐｙｔｈｏｎ ', 'Python', 'software engineer'], ['remote']], exclusions: ['intern'] } });
    items = [{ ...search(), ...body, definition: { ...body.definition, groups: [['Python', 'Python', 'software engineer'], ['remote']] } }];
    return HttpResponse.json(items[0], { status: 201 });
  }), http.put(path + '/:id', async ({ request, params }) => {
    writes++; const body = await request.json() as components['schemas']['SearchEdit'];
    expect(params.id).toBe(items[0]?.id); expect(body.expected_revision).toBe(1);
    expect(body.definition).toEqual(items[0]?.definition); expect(body.name).toBe('Senior roles');
    items = [{ ...search(), ...items[0], name: body.name, definition: body.definition, revision: 2 }]; return HttpResponse.json(items[0]);
  }), http.delete(path + '/:id', ({ request, params }) => {
    writes++; expect(params.id).toBe(items[0]?.id); expect(new URL(request.url).searchParams.get('expected_revision')).toBe('2');
    expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token); items = []; return new HttpResponse(null, { status: 204 });
  }));
  const view = mount(); await user.click(await screen.findByRole('button', { name: 'New search' }));
  await user.type(field('Search name'), 'Backend roles'); await user.type(field('Group 1 term 1'), ' Ｐｙｔｈｏｎ ');
  button('Add OR term to group 1').focus(); await user.keyboard('{Enter}'); expect(field('Group 1 term 2')).toHaveFocus(); await user.keyboard('Python');
  expect(screen.getByText(/Repeated terms/)).toBeVisible(); await user.click(button('Add OR term to group 1')); await user.keyboard('software engineer');
  await user.click(button('Add AND group')); expect(field('Group 2 term 1')).toHaveFocus(); await user.keyboard('remote');
  await user.click(button('Add exclusion')); expect(field('Exclusion 1')).toHaveFocus(); await user.keyboard('intern');
  expect(screen.getByLabelText('Search logic')).toHaveTextContent('Python OR software engineer');
  expect(screen.getByLabelText('Search logic')).toHaveTextContent('ANDMatch any of: remote');
  await user.click(button('Save search')); expect(await screen.findByText('Saved “Backend roles”.')).toBeVisible();
  view.unmount(); mount(); await user.click(await screen.findByRole('button', { name: 'Edit Backend roles' }));
  expect(field('Group 1 term 1')).toHaveValue('Python'); expect(field('Group 1 term 2')).toHaveValue('Python'); expect(field('Exclusion 1')).toHaveValue('intern');
  await user.clear(field('Search name')); await user.type(field('Search name'), 'Senior roles'); await user.click(button('Save search'));
  expect(await screen.findByText('Saved “Senior roles”.')).toBeVisible(); await user.click(button('Delete Senior roles'));
  expect(button('Keep search')).toHaveFocus(); await user.click(button('Keep search')); expect(button('Delete Senior roles')).toHaveFocus();
  await user.click(button('Delete Senior roles')); await user.click(button('Confirm deletion'));
  expect(await screen.findByText('Search deleted. Existing runs are kept.')).toBeVisible(); await waitFor(() => { expect(button('New search')).toHaveFocus(); });
  expect(screen.getByText('No saved searches yet')).toBeVisible(); expect(writes).toBe(3);
});

test.each(['groups', 'terms', 'total', 'exclusions'] as const)('bounds %s and restores space after removal', async limit => {
  const groups = limit === 'groups' ? Array.from({ length: 6 }, () => ['Python']) : limit === 'total'
    ? Array.from({ length: 4 }, () => Array.from({ length: 8 }, (_, i) => `Term ${String(i)}`))
    : [limit === 'terms' ? Array.from({ length: 8 }, (_, i) => `Term ${String(i)}`) : ['Python']];
  items = [search({ groups, exclusions: limit === 'exclusions' ? Array.from({ length: 8 }, (_, i) => `Exclude ${String(i)}`) : [] })];
  const user = await edit();
  if (limit === 'groups') { expect(button('Add AND group')).toBeDisabled(); await user.click(button('Remove group 3')); expect(field('Group 3 term 1')).toHaveFocus(); }
  if (limit === 'terms' || limit === 'total') {
    expect(button('Add OR term to group 1')).toBeDisabled(); if (limit === 'total') expect(button('Add AND group')).toBeDisabled();
    await user.click(button('Remove term 8 from group 1')); expect(field('Group 1 term 7')).toHaveFocus(); expect(button('Add OR term to group 1')).toBeEnabled();
  }
  if (limit === 'exclusions') { expect(button('Add exclusion')).toBeDisabled(); await user.click(button('Remove exclusion 8')); expect(field('Exclusion 7')).toHaveFocus(); expect(button('Add exclusion')).toBeEnabled(); }
  expect(button('Add AND group')).toBeEnabled();
});

test('associates empty, excessive and Unicode-normalized length errors and maintains remove focus', async () => {
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'New search' }));
  await user.click(button('Save search')); expect(field('Search name')).toHaveFocus(); expect(field('Search name')).toHaveAccessibleDescription(/1–120 characters/);
  expect(field('Group 1 term 1')).toHaveAttribute('aria-invalid', 'true');
  fireEvent.change(field('Search name'), { target: { value: 'x'.repeat(121) } }); await user.click(button('Save search')); expect(field('Search name')).toHaveFocus();
  await user.clear(field('Search name')); await user.type(field('Search name'), 'Valid');
  fireEvent.change(field('Group 1 term 1'), { target: { value: 'x'.repeat(81) } }); await user.click(button('Save search')); expect(field('Group 1 term 1')).toHaveFocus();
  fireEvent.change(field('Group 1 term 1'), { target: { value: 'ﬃ'.repeat(27) } }); await user.click(button('Save search')); expect(field('Group 1 term 1')).toHaveAccessibleDescription(/1–80 characters/);
  await user.clear(field('Group 1 term 1')); await user.type(field('Group 1 term 1'), 'Python');
  await user.click(button('Add exclusion')); await user.click(button('Save search')); expect(field('Exclusion 1')).toHaveFocus();
  await user.click(button('Remove exclusion 1')); expect(button('Add exclusion')).toHaveFocus();
  await user.click(button('Add OR term to group 1')); await user.click(button('Remove term 1 from group 1')); expect(field('Group 1 term 1')).toHaveFocus();
  await user.click(button('Add AND group')); await user.click(button('Remove group 2')); expect(field('Group 1 term 1')).toHaveFocus();
});

test('corrects a safe validation error and prevents duplicate submission while pending', async () => {
  items = [search()]; const deferred = gate(); let writes = 0;
  server.use(http.put(path + '/:id', async () => { writes++; if (writes === 1) return failure(422); await deferred.pending; return HttpResponse.json(items[0]); }));
  const user = await edit(); await user.type(field('Group 1 term 1'), ':invalid'); await user.click(button('Save search'));
  expect(await screen.findByText(/Some values were rejected/)).toBeVisible(); expect(screen.queryByText('Private server message')).not.toBeInTheDocument();
  expect(field('Group 1 term 1')).toHaveValue('Python:invalid'); expect(button('Save search')).toBeEnabled();
  await user.clear(field('Group 1 term 1')); await user.type(field('Group 1 term 1'), 'Rust'); await user.click(button('Save search'));
  expect(await screen.findByRole('button', { name: 'Saving search…' })).toBeDisabled(); expect(field('Search name')).toBeDisabled(); expect(button('Cancel editing')).toBeDisabled();
  const form = field('Search name').closest('form'); if (!form) throw new Error('Missing form'); fireEvent.submit(form); expect(writes).toBe(2);
  deferred.release(); expect(await screen.findByText('Saved “Engineering”.')).toBeVisible();
});

test('reloads a stale revision without losing any draft fields or latest discovery hints', async () => {
  items = [search({ groups: [['Python']], sites: ['linkedin.com'], language: 'fr', country: 'fr', location: 'Paris', recency_days: 7 })];
  let writes = 0, reads = 0;
  server.use(http.put(path + '/:id', async ({ request }) => {
    writes++; const body = await request.json();
    if (writes === 1) { items = [{ ...items[0] as SavedSearch, name: 'Elsewhere', revision: 8, definition: { ...items[0]?.definition, groups: [['Go']], country: 'gb' } }]; return failure(409); }
    expect(body).toEqual({ name: 'My draft', expected_revision: 8, definition: { ...items[0]?.definition, groups: [['Python', 'Rust'], ['remote']], exclusions: ['intern'] } });
    return HttpResponse.json({ ...items[0], name: 'My draft', revision: 9 });
  }), http.get(path + '/:id', () => { reads++; return reads === 1 ? failure(503) : HttpResponse.json(items[0]); }));
  const user = await edit(); await user.clear(field('Search name')); await user.type(field('Search name'), 'My draft');
  await user.click(button('Add OR term to group 1')); await user.keyboard('Rust'); await user.click(button('Add AND group')); await user.keyboard('remote');
  await user.click(button('Add exclusion')); await user.keyboard('intern'); await user.click(button('Save search'));
  expect(await screen.findByText(/The save could not be confirmed/)).toBeVisible(); expect(button('Save search')).toBeDisabled();
  await user.click(button('Reload saved searches')); expect(await screen.findByText(/The latest search could not be loaded/)).toBeVisible();
  expect(button('Save search')).toBeDisabled(); await user.click(button('Reload saved searches'));
  expect(await screen.findByText('Latest saved version: Elsewhere')).toBeVisible(); expect(field('Search name')).toHaveValue('My draft');
  expect(field('Group 1 term 2')).toHaveValue('Rust'); expect(field('Group 2 term 1')).toHaveValue('remote'); expect(field('Exclusion 1')).toHaveValue('intern');
  expect(writes).toBe(1); await user.click(button('Save search')); expect(await screen.findByText('Saved “My draft”.')).toBeVisible(); expect(writes).toBe(2);
});

test('keeps a deleted search draft available for review and cancellation', async () => {
  items = [search()]; server.use(http.put(path + '/:id', () => { items = []; return failure(404); }));
  const user = await edit(); await user.type(field('Search name'), ' draft'); await user.click(button('Save search'));
  await user.click(await screen.findByRole('button', { name: 'Reload saved searches' }));
  expect(await screen.findByText(/This search was deleted elsewhere/)).toBeVisible(); expect(field('Search name')).toHaveValue('Engineering draft');
  expect(button('Save search')).toBeDisabled(); await user.click(button('Cancel editing')); expect(button('New search')).toHaveFocus();
});

test.each(['network', 'server'])('requires explicit review after an uncertain %s create failure', async kind => {
  let writes = 0; server.use(http.post(path, () => { writes++; return kind === 'network' ? HttpResponse.error() : failure(503); }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'New search' }));
  await user.type(field('Search name'), 'My draft'); await user.type(field('Group 1 term 1'), 'Python'); await user.click(button('Save search'));
  expect(await screen.findByText(/The save could not be confirmed/)).toBeVisible(); expect(button('Save search')).toBeDisabled();
  await user.click(button('Reload saved searches')); expect(await screen.findByText(/Latest searches loaded/)).toBeVisible();
  expect(field('Search name')).toHaveValue('My draft'); expect(field('Group 1 term 1')).toHaveValue('Python'); expect(button('Save search')).toBeEnabled(); expect(writes).toBe(1);
});

test('recovers list failure, pages results and keeps drafts through background failures', async () => {
  let broken = true; items = [search()];
  server.use(http.get(path, ({ request }) => broken ? failure(503) : HttpResponse.json(new URL(request.url).searchParams.get('offset') === '20'
    ? { items: [], next_offset: null } : { items, next_offset: 20 })));
  const user = userEvent.setup(); mount(); expect(await screen.findByText('Saved searches could not be loaded')).toBeVisible(); expect(button('New search')).toBeDisabled();
  await user.click(button('Reload searches')); expect(await screen.findByText('The list could not be reloaded. Try again.')).toBeVisible();
  broken = false; await user.click(button('Reload searches')); await user.click(await screen.findByRole('button', { name: 'Next searches' }));
  expect(await screen.findByText('No searches on this page')).toBeVisible(); expect(button('Next searches')).toBeDisabled();
  await user.click(button('Previous searches')); await user.click(await screen.findByRole('button', { name: 'Edit Engineering' }));
  await user.type(field('Search name'), ' unsaved'); broken = true;
  await act(async () => { await api.queryClient.invalidateQueries({ queryKey: ['searches'] }); });
  expect(await screen.findByText('Saved searches could not be loaded')).toBeVisible(); expect(field('Search name')).toHaveValue('Engineering unsaved');
  broken = false; await user.click(button('Reload searches')); expect(field('Search name')).toHaveValue('Engineering unsaved'); await user.click(button('Cancel editing'));
});

test('requires a fresh delete confirmation after conflicts and survives a disappearing list row', async () => {
  items = [search()]; const deferred = gate(); let writes = 0;
  server.use(http.delete(path + '/:id', async ({ request }) => {
    writes++; if (writes === 1) { items = [{ ...items[0] as SavedSearch, revision: 2 }]; return failure(409); }
    expect(new URL(request.url).searchParams.get('expected_revision')).toBe('2'); await deferred.pending; items = []; return new HttpResponse(null, { status: 204 });
  }));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Delete Engineering' })); await user.click(button('Confirm deletion'));
  expect(await screen.findByText(/Deletion could not be confirmed/)).toBeVisible(); expect(button('Confirm deletion')).toBeDisabled();
  await user.click(button('Reload before deleting')); expect(await screen.findByText(/Review the latest version before deleting/)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Confirm deletion' })).not.toBeInTheDocument(); expect(writes).toBe(1);
  await user.click(button('Delete Engineering')); const original = items; items = [];
  await act(async () => { await api.queryClient.invalidateQueries({ queryKey: ['searches'] }); });
  expect(button('Keep search')).toBeVisible(); await user.click(button('Keep search')); expect(button('New search')).toHaveFocus();
  items = original; await act(async () => { await api.queryClient.invalidateQueries({ queryKey: ['searches'] }); });
  await user.click(button('Delete Engineering')); const confirm = button('Confirm deletion'); fireEvent.click(confirm); fireEvent.click(confirm);
  expect(await screen.findByRole('button', { name: 'Deleting search…' })).toBeDisabled(); expect(button('Keep search')).toBeDisabled();
  deferred.release(); expect(await screen.findByText('Search deleted. Existing runs are kept.')).toBeVisible(); expect(writes).toBe(2);
});

test.each(['list', 'save', 'delete', 'reload'] as const)('clears private state when the session expires during %s', async operation => {
  items = [search()]; const user = userEvent.setup();
  if (operation === 'list') server.use(http.get(path, () => { current = null; return failure(401); }));
  else if (operation === 'delete') server.use(http.delete(path + '/:id', () => { current = null; return failure(403); }));
  else server.use(http.put(path + '/:id', () => { if (operation === 'save') current = null; return failure(operation === 'save' ? 401 : 409); }),
    http.get(path + '/:id', () => { current = null; return failure(401); }));
  mount();
  if (operation === 'delete') { await user.click(await screen.findByRole('button', { name: 'Delete Engineering' })); await user.click(button('Confirm deletion')); }
  if (operation === 'save' || operation === 'reload') {
    await user.click(await screen.findByRole('button', { name: 'Edit Engineering' })); await user.click(button('Save search'));
    if (operation === 'reload') await user.click(await screen.findByRole('button', { name: 'Reload saved searches' }));
  }
  expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeVisible(); expect(screen.queryByDisplayValue('Engineering')).not.toBeInTheDocument();
  expect(api.queryClient.getQueryData(['searches', owner.id, 0])).toBeUndefined();
});

test('aborts a departing owner’s pending save and never shows their draft to the next owner', async () => {
  items = [search()]; const deferred = gate(); let aborted = false;
  server.use(http.put(path + '/:id', async ({ request }) => { request.signal.addEventListener('abort', () => { aborted = true; }); await deferred.pending; return HttpResponse.json(search()); }));
  const user = await edit(); await user.type(field('Search name'), ' private'); await user.click(button('Save search'));
  expect(await screen.findByRole('button', { name: 'Saving search…' })).toBeDisabled();
  current = { ...owner, id: crypto.randomUUID(), email: 'other@example.org' }; items = [];
  await act(async () => { await api.invalidateSession(); }); expect(await screen.findByText('No saved searches yet')).toBeVisible();
  expect(screen.queryByDisplayValue('Engineering private')).not.toBeInTheDocument(); expect(aborted).toBe(true); deferred.release();
  expect(api.queryClient.getQueryData(['searches', owner.id, 0])).toBeUndefined();
  expect(within(screen.getByRole('main')).queryByText('Saved “Engineering”.')).not.toBeInTheDocument();
});
