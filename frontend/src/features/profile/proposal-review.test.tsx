import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterAll, afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest';
import type { components } from '../../../generated/api';
import { api, type Session } from '../../shared/api';
import { server } from '../../test/setup';
import { ProposalReview } from './ProposalReview';
import { Documents } from '../documents/Documents';

type Candidate = components['schemas']['Candidate'];
type Proposal = components['schemas']['Proposal'];
type Page = components['schemas']['ProposalPage'];
type Snapshot = components['schemas']['ProfileSnapshot'];
type Batch = components['schemas']['ReviewBatch'];
const owner: Session = { id: crypto.randomUUID(), email: 'reviewer@example.org', email_verified: true, csrf_token: 'r'.repeat(64) };
const documentId = crypto.randomUUID(), profilePath = 'http://localhost/api/v1/profile';
const proposalPath = `http://localhost/api/v1/documents/${documentId}/proposals`;
const hostile = '<img src=x onerror=alert(1)>';
let page: Page, snapshot: Snapshot, batches: Batch[];
function proposal(kind: Candidate['kind'], name: string): Proposal {
  const fields: Record<string, string | boolean | null> = kind === 'skill' ? { name }
    : { ...(kind === 'experience' ? { company: 'Acme', title: name } : { name, url: 'https://example.org/Project' }),
      start_date: null, end_date: null, ongoing: null, description: hostile };
  return { id: crypto.randomUUID(), candidate: { kind, fields: Object.entries(fields).map(([field, value]) => ({
    name: field as Candidate['fields'][number]['name'], value, uncertainty: value === null ? 'missing' : field === 'description' ? 'uncertain' : 'explicit',
    evidence: value === null ? [] : [{ locator: 'paragraph:1', start: 0, end: String(value).length, quote: String(value) }],
  })) } };
}
function gate() { let release: () => void = () => undefined; const pending = new Promise<void>(resolve => { release = resolve; }); return { pending, release }; }
const failure = (status: number) => HttpResponse.json({ code: 'fixture', message: 'Private failure', field_errors: [], request_id: documentId }, { status });
function tree(id = owner.id) { return <QueryClientProvider client={api.queryClient}><ProposalReview key={id} ownerId={id} documentId={documentId} filename="résumé.txt" /></QueryClientProvider>; }
async function mount() { const view = render(tree()); await screen.findByRole('checkbox', { name: 'Select proposed skill: Python' }); return view; }
const choose = (name = 'Select proposed skill: Python') => screen.getByRole('checkbox', { name });
const accept = () => screen.getByRole('button', { name: 'Accept selected' });
beforeAll(() => { Object.defineProperties(HTMLDialogElement.prototype, {
  showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true; } },
  close: { configurable: true, value(this: HTMLDialogElement) { this.open = false; } },
}); });
afterAll(() => { Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal'); Reflect.deleteProperty(HTMLDialogElement.prototype, 'close'); });
beforeEach(() => {
  api.clearSession(); api.queryClient.clear(); api.acceptSession(owner); batches = [];
  page = { status: 'ready', source_version: 1, language: 'fr', count: 3, items: [proposal('skill', 'Python'), proposal('experience', 'Engineer'), proposal('project', 'Atlas')] };
  snapshot = { id: owner.id, revision: 1, facts: [{ id: crypto.randomUUID(), data: { kind: 'skill', name: 'PYTHON' }, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' }] };
  server.use(http.get(profilePath, () => HttpResponse.json(snapshot)), http.get(proposalPath, () => HttpResponse.json(page)),
    http.post(`${profilePath}/review`, async ({ request }) => {
      expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token); const body = await request.json() as Batch; batches.push(body);
      snapshot = { ...snapshot, revision: snapshot.revision + 1 };
      return HttpResponse.json({ revision: snapshot.revision, items: body.items.map(item => ({ proposal_id: item.proposal_id,
        outcome: item.action === 'reject' ? 'rejected' : item.proposal_id === page.items?.[0]?.id ? 'linked' : 'created', fact_id: crypto.randomUUID() })) });
    }));
});
afterEach(() => { api.clearSession(); api.queryClient.clear(); });

test('shows exact hostile excerpts as text, uncertainty, source version/language and existing-fact suggestions', async () => {
  const user = userEvent.setup(); await mount(); expect(screen.getByText('Source version 1 · Language: fr')).toBeVisible();
  expect(screen.getAllByText(hostile).length).toBeGreaterThan(1); expect(screen.queryByRole('img')).not.toBeInTheDocument();
  expect(screen.getAllByText('Uncertain')).toHaveLength(2); expect(screen.getAllByText('Missing').length).toBeGreaterThan(1);
  await user.click(screen.getByText('Existing facts with matching identifying values (1)'));
  expect(screen.getByText('PYTHON')).toBeVisible(); expect(screen.getAllByText(/paragraph:1 · Characters/).length).toBeGreaterThan(1);
  expect(accept()).toBeDisabled(); expect(batches).toEqual([]);
});

test('accepts and rejects selected candidates with created/linked/rejected feedback and keyboard selection', async () => {
  const user = userEvent.setup(); await mount(); choose().focus(); await user.keyboard(' ');
  await user.click(choose('Select proposed experience: Engineer')); await user.click(accept());
  expect(await screen.findByText('1 created · 1 linked · 0 rejected · 0 conflict')).toBeVisible();
  expect(choose()).toBeDisabled(); expect(choose()).not.toBeChecked();
  await user.click(choose('Select proposed project: Atlas')); await user.click(screen.getByRole('button', { name: 'Reject selected' }));
  expect(await screen.findByText('Rejected', { exact: true })).toBeVisible(); expect(batches[1]?.items[0]?.action).toBe('reject');
  expect(batches[1]?.expected_revision).toBe(2);
});

test.each([['skill', 'Skill name', 'Rust'], ['experience', 'Company', 'Studio'], ['project', 'Project name', 'Edited Atlas']] as const)(
  'edits a proposed %s through the shared editor and keeps a draft until explicit acceptance', async (kind, label, value) => {
    const user = userEvent.setup(); await mount(); const opener = screen.getByRole('button', { name: `Edit proposed ${kind}` }); await user.click(opener);
    const dialog = screen.getByRole('dialog', { name: `Edit proposed ${kind}` });
    const input = within(dialog).getByLabelText(label, { exact: false }); expect(input).toHaveFocus();
    await user.clear(input); await user.click(within(dialog).getByRole('button', { name: 'Keep draft' })); expect(input).toHaveFocus();
    expect(within(dialog).getByRole('alert')).toHaveTextContent(kind === 'skill' ? '1–120' : '1–200');
    await user.type(input, value); await user.click(within(dialog).getByRole('button', { name: 'Keep draft' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(opener).toHaveFocus();
    expect(batches).toEqual([]); expect(screen.getByText('Your draft correction')).toBeVisible();
    await user.click(accept()); await waitFor(() => { expect(batches).toHaveLength(1); });
    expect(batches[0]?.items[0]).toMatchObject({ action: 'edit', fact: { kind, [kind === 'experience' ? 'company' : 'name']: value } });
  },
);

test('cancels editing and retains a correction when a candidate is deselected and reselected', async () => {
  const user = userEvent.setup(); await mount(); await user.click(screen.getByRole('button', { name: 'Edit proposed skill' }));
  await user.click(screen.getByRole('button', { name: 'Cancel' })); expect(choose()).not.toBeChecked();
  await user.click(screen.getByRole('button', { name: 'Edit proposed skill' })); await user.type(screen.getByLabelText('Skill name', { exact: false }), ' advanced');
  await user.click(screen.getByRole('button', { name: 'Keep draft' })); await user.click(choose()); await user.click(choose()); await user.click(accept());
  await waitFor(() => { expect(batches[0]?.items[0]?.fact).toMatchObject({ name: 'Python advanced' }); });
});

test('keeps unresolved selections after a partial result while completed decisions cannot be resubmitted', async () => {
  const skill = page.items?.[0], experience = page.items?.[1];
  server.use(http.post(`${profilePath}/review`, () => HttpResponse.json({ revision: 2, items: [
    { proposal_id: skill?.id, outcome: 'linked' }, { proposal_id: experience?.id, outcome: 'conflict' },
  ] })));
  const user = userEvent.setup(); await mount(); await user.click(choose()); await user.click(choose('Select proposed experience: Engineer')); await user.click(accept());
  expect(await screen.findByText('0 created · 1 linked · 0 rejected · 1 conflict')).toBeVisible();
  expect(choose()).toBeDisabled(); expect(choose('Select proposed experience: Engineer')).toBeChecked();
  expect(screen.getByText(/Conflict: not applied/)).toBeVisible();
});

test.each([409, 422])('keeps choices after %i, requires reload and submits with the new revision and request identity', async status => {
  server.use(http.post(`${profilePath}/review`, async ({ request }) => {
    batches.push(await request.json() as Batch);
    return batches.length === 1 ? failure(status) : HttpResponse.json({ revision: 8, items: [{ proposal_id: page.items?.[0]?.id, outcome: 'linked' }] });
  }));
  const user = userEvent.setup(); await mount(); await user.click(choose()); await user.click(accept());
  expect(await screen.findByRole('alert')).toHaveTextContent('profile revision changed'); expect(choose()).toBeChecked(); expect(accept()).toBeDisabled();
  snapshot = { ...snapshot, revision: 7 }; await user.click(screen.getByRole('button', { name: 'Reload profile and candidates' }));
  await waitFor(() => { expect(accept()).toBeEnabled(); }); await user.click(accept()); await waitFor(() => { expect(batches).toHaveLength(2); });
  expect(batches[1]?.expected_revision).toBe(7); expect(batches[1]?.items).toEqual(batches[0]?.items);
  expect(batches[1]?.idempotency_key).not.toBe(batches[0]?.idempotency_key);
});

test('retries an unconfirmed result with the identical payload and prevents double submission or changed choices', async () => {
  const pending = gate(); server.use(http.post(`${profilePath}/review`, async ({ request }) => {
    batches.push(await request.json() as Batch); await pending.pending;
    return batches.length === 1 ? failure(503) : HttpResponse.json({ revision: 2, items: [{ proposal_id: page.items?.[0]?.id, outcome: 'linked' }] });
  }));
  const user = userEvent.setup(); await mount(); await user.click(choose()); await user.dblClick(accept());
  await waitFor(() => { expect(batches).toHaveLength(1); }); expect(choose()).toBeDisabled(); pending.release();
  expect(await screen.findByRole('alert')).not.toHaveTextContent('Private failure');
  await user.click(screen.getByRole('button', { name: 'Retry same decisions' }));
  expect(await screen.findByText('Accepted: linked to an existing fact')).toBeVisible(); expect(batches[1]).toEqual(batches[0]);
});

test('keeps selected candidates across pages and enforces 100 selected per request', async () => {
  const first = Array.from({ length: 100 }, (_, index) => proposal('skill', index ? `Skill ${String(index)}` : 'Python'));
  server.use(http.get(proposalPath, ({ request }) => HttpResponse.json({ ...page, count: 101,
    items: new URL(request.url).searchParams.get('offset') === '0' ? first : [proposal('skill', 'Last')] })));
  const user = userEvent.setup(); await mount(); act(() => { for (const checkbox of screen.getAllByRole('checkbox')) fireEvent.click(checkbox); });
  expect(screen.getByText('100 selected across pages (maximum 100 per submission).')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Next candidates' })); expect(await screen.findByRole('checkbox', { name: 'Select proposed skill: Last' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Previous candidates' })); expect(await screen.findByRole('checkbox', { name: 'Select proposed skill: Python' })).toBeChecked();
}, 10000);

test.each(['waiting_for_parse', 'pending', 'running', 'failed', 'unavailable', 'ready'] as const)('shows the %s state without inventing candidates', async status => {
  page = { status, source_version: 1 }; render(tree()); await screen.findByText('Source version 1 · Language: Unknown');
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  if (status === 'ready') expect(screen.getByText(/No candidates on this page/)).toBeVisible();
});

test('recovers failed reads and preserves selected values when refreshing fails', async () => {
  server.use(http.get(proposalPath, () => failure(503))); const user = userEvent.setup(); render(tree());
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be loaded');
  server.use(http.get(proposalPath, () => HttpResponse.json(page))); await user.click(screen.getByRole('button', { name: 'Reload profile and candidates' }));
  await user.click(await screen.findByRole('checkbox', { name: 'Select proposed skill: Python' }));
  server.use(http.get(profilePath, () => failure(503))); await user.click(screen.getByRole('button', { name: 'Reload profile and candidates' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('selections and drafts are kept'); expect(choose()).toBeChecked(); expect(accept()).toBeDisabled();
});

test.each([401, 403, 404])('handles access loss %i without exposing candidate content as a successful result', async status => {
  const invalidate = vi.spyOn(api, 'invalidateSession').mockResolvedValue(); server.use(http.get(proposalPath, () => failure(status)));
  render(tree()); await screen.findByRole('alert'); expect(screen.queryByText(hostile)).not.toBeInTheDocument();
  if (status === 404) expect(screen.getByText(/document or a selected candidate is unavailable/)).toBeVisible();
  else await waitFor(() => { expect(invalidate).toHaveBeenCalled(); });
});

test('aborts submission and clears private drafts on owner change; a late result cannot enter the new account', async () => {
  const pending = gate(); let signal: AbortSignal | undefined;
  server.use(http.post(`${profilePath}/review`, async ({ request }) => { signal = request.signal; await pending.pending; return HttpResponse.json({ revision: 2, items: [] }); }));
  const user = userEvent.setup(); const view = await mount(); await user.click(choose()); await user.click(accept()); await waitFor(() => { expect(signal).toBeDefined(); });
  page = { status: 'ready', source_version: 1 }; act(() => { api.acceptSession({ ...owner, id: 'next-owner' }); }); view.rerender(tree('next-owner')); pending.release();
  expect(signal?.aborted).toBe(true); await screen.findByText(/No candidates on this page/);
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument(); expect(api.queryClient.getQueriesData({ queryKey: ['documents', owner.id] })).toEqual([]);
});

test('deleting a document during review aborts its submission and removes cached proposals', async () => {
  const document: components['schemas']['Document'] = { id: documentId, filename: 'résumé.txt', status: 'ready', media_type: 'text/plain',
    size_bytes: 100, version: 1, created_at: '2026-01-01T00:00:00Z', deleted_at: null };
  const pending = gate(); let signal: AbortSignal | undefined, deleted = false;
  server.use(http.get('http://localhost/api/v1/documents', () => HttpResponse.json(deleted ? [] : [document])),
    http.get(`http://localhost/api/v1/documents/${documentId}`, () => HttpResponse.json(document)),
    http.post(`${profilePath}/review`, async ({ request }) => { signal = request.signal; await pending.pending; return HttpResponse.json({ revision: 2, items: [] }); }),
    http.delete(`http://localhost/api/v1/documents/${documentId}`, () => { deleted = true; return HttpResponse.json({ id: documentId, status: 'deleted' }); }));
  const user = userEvent.setup(); render(<QueryClientProvider client={api.queryClient}><Documents ownerId={owner.id} /></QueryClientProvider>);
  await user.click(await screen.findByRole('button', { name: 'Review candidates' }));
  await user.click(await screen.findByRole('checkbox', { name: 'Select proposed skill: Python' })); await user.click(accept());
  await waitFor(() => { expect(signal).toBeDefined(); }); await user.click(screen.getByRole('button', { name: 'Delete résumé.txt' }));
  await user.click(screen.getByRole('button', { name: 'Confirm deletion' })); await screen.findByText('Deleted', { exact: true });
  expect(signal?.aborted).toBe(true); pending.release(); expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  expect(api.queryClient.getQueriesData({ queryKey: ['documents', owner.id, 'review', documentId] })).toEqual([]);
});
