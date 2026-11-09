import { QueryClientProvider, focusManager } from '@tanstack/react-query';
import { File as FetchFile } from 'node:buffer';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { components } from '../../../generated/api';
import { api, type Session } from '../../shared/api';
import { server } from '../../test/setup';
import { Documents } from './Documents';

type Document = components['schemas']['Document'];
type State = Document | components['schemas']['DeletionState'];
type Proposals = components['schemas']['ProposalPage'];
const owner: Session = { id: '00000000-0000-4000-8000-000000000001', email: 'owner@example.org', email_verified: true, csrf_token: 'c'.repeat(64) };
const base = 'http://localhost/api/v1/documents';
const document: Document = { id: '00000000-0000-4000-8000-000000000002', filename: 'résumé.txt', media_type: 'text/plain', size_bytes: 1234, version: 1, status: 'queued', created_at: '2026-01-01T00:00:00Z', deleted_at: null };
const failure = (status: number) => HttpResponse.json({ code: 'fixture', message: 'Private server message', field_errors: [], request_id: document.id }, { status });
let records: Document[], state: State, proposals: Proposals, reads: number, extractions: number;
function gate() { let release: () => void = () => undefined; const pending = new Promise<void>(resolve => { release = resolve; }); return { pending, release }; }
function tree(id = owner.id) { return <QueryClientProvider client={api.queryClient}><Documents key={id} ownerId={id} /></QueryClientProvider>; }
function mount() { return render(tree()); }
const picker = () => screen.getByLabelText('Choose a document, or drop one here');
const uploadButton = () => screen.getByRole('button', { name: 'Upload document' });
beforeEach(() => {
  // Node fetch needs a stream-capable File; jsdom's File does not implement stream().
  vi.stubGlobal('File', FetchFile);
  api.clearSession(); api.queryClient.clear(); api.acceptSession(owner);
  records = []; state = document; proposals = { status: 'pending', source_version: 1 }; reads = 0; extractions = 0;
  server.use(http.get(base, () => HttpResponse.json(records)),
    http.get(`${base}/${document.id}`, () => { reads++; return HttpResponse.json(state); }),
    http.get(`${base}/${document.id}/proposals`, () => { extractions++; return HttpResponse.json(proposals); }));
});
afterEach(() => { api.clearSession(); api.queryClient.clear(); focusManager.setFocused(undefined); vi.useRealTimers(); vi.unstubAllGlobals(); });

test('keyboard picker uploads once with CSRF, encoded filename, selected type and indeterminate progress', async () => {
  const user = userEvent.setup(), pending = gate(); let calls = 0;
  server.use(http.post(base, async ({ request }) => {
    calls++; expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    expect(request.headers.get('x-document-name')).toBe('r%C3%A9sum%C3%A9.txt');
    expect(request.headers.get('content-type')).toBe('text/plain');
    expect(await request.text()).toBe('Profile');
    await pending.pending; records = [document]; return HttpResponse.json(document, { status: 201 });
  }));
  mount(); await screen.findByText(/No documents yet/); await user.tab(); expect(picker()).toHaveFocus();
  await user.upload(picker(), new File(['Profile'], 'résumé.txt')); await user.tab(); await user.keyboard('{Enter}');
  expect(await screen.findByText('Uploading your document. Progress is not measurable.')).toBeVisible();
  expect(picker()).toBeDisabled(); expect(screen.getByRole('button', { name: 'Uploading…' })).toBeDisabled();
  fireEvent.submit(picker().closest('form') as HTMLFormElement); await waitFor(() => { expect(calls).toBe(1); });
  pending.release(); expect(await screen.findByText(/Document uploaded/)).toBeVisible();
  expect(await screen.findByRole('heading', { name: document.filename })).toBeVisible();
  expect(screen.getByRole('link', { name: `Download ${document.filename}` })).toHaveAttribute('href', `/api/v1/documents/${document.id}/download`);
  expect(picker()).toHaveValue(''); expect(screen.queryByText(/Selected:/)).not.toBeInTheDocument();
});

test.each(['pdf', 'docx', 'txt'])('accepts %s drops and reports an existing document without duplicating rows', async extension => {
  server.use(http.post(base, () => HttpResponse.json(document))); records = [document];
  const user = userEvent.setup(); mount(); await screen.findByRole('heading', { name: document.filename });
  fireEvent.drop(picker().closest('form') as HTMLFormElement, { dataTransfer: { files: [new File(['data'], `other.${extension}`)] } });
  await user.click(uploadButton()); expect(await screen.findByText(/This document already exists/)).toBeVisible();
  expect(screen.getAllByRole('heading', { name: document.filename })).toHaveLength(1);
});

test('rejects missing, multiple, empty, unsupported and oversized files locally; cancellation clears selection', async () => {
  const user = userEvent.setup({ applyAccept: false }); mount(); await user.click(uploadButton());
  expect(picker()).toHaveFocus(); expect(await screen.findByText('Choose a document to upload.')).toBeVisible();
  for (const file of [new File(['data'], 'bad.exe'), new File([], 'empty.txt'), Object.defineProperty(new File(['a'], 'huge.txt'), 'size', { value: 52428801 })]) {
    await user.upload(picker(), file); expect(screen.getByRole('alert')).toHaveTextContent('non-empty PDF'); expect(picker()).toHaveAttribute('aria-invalid', 'true');
  }
  fireEvent.drop(picker().closest('form') as HTMLFormElement, { dataTransfer: { files: [new File(['a'], 'a.txt'), new File(['b'], 'b.txt')] } });
  expect(screen.getByRole('alert')).toHaveTextContent('one document');
  fireEvent.change(picker(), { target: { files: null } }); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test.each([413, 415, 409, 503])('shows safe upload error %i and explicitly retries the kept selection', async status => {
  let calls = 0; server.use(http.post(base, () => ++calls === 1 ? failure(status) : HttpResponse.json(document)));
  const user = userEvent.setup(); mount(); await user.upload(picker(), new File(['a'], 'a.txt')); await user.click(uploadButton());
  expect(await screen.findByRole('alert')).not.toHaveTextContent('Private server message');
  if (status === 409) expect(screen.getByRole('alert')).toHaveTextContent('deletion of this file is still in progress');
  expect(screen.getByText('Selected: a.txt')).toBeVisible(); expect(calls).toBe(1);
  await user.click(screen.getByRole('button', { name: 'Retry upload' })); expect(await screen.findByText(/already exists/)).toBeVisible(); expect(calls).toBe(2);
});

test.each(['ocr_required', 'encrypted_document', 'empty_document', 'limits_exceeded', 'timed_out', 'cancelled', 'storage_unavailable', 'unsupported_format', 'invalid_document'] as const)(
  'renders terminal parsing guidance for %s', async parse_error => {
    state = { ...document, filename: '<img src=x onerror=alert(1)>.pdf', status: 'unsupported', parse_error }; records = [state]; mount();
    expect(await screen.findByText('Unsupported')).toBeVisible();
    expect(screen.getByRole('heading', { name: state.filename })).toBeVisible(); expect(screen.queryByRole('img')).not.toBeInTheDocument();
    if (parse_error === 'ocr_required') expect(screen.getByText(/This scanned PDF needs OCR/)).toBeVisible();
    expect(extractions).toBe(0);
  },
);

test.each(['unavailable', 'failed', 'ready'] as const)('shows extraction %s and explicit unknown language without caching candidate text', async status => {
  state = { ...document, status: 'ready' }; records = [state]; proposals = { status, source_version: 1 }; mount();
  await waitFor(() => { expect(extractions).toBe(1); });
  if (status === 'ready') expect(await screen.findByText(/0 candidates · Language: Unknown/)).toBeVisible();
  else expect(await screen.findByText(status === 'failed' ? /Extraction failed/ : /Extraction unavailable/)).toBeVisible();
  expect(api.queryClient.getQueryData(['documents', owner.id, 'extraction', document.id])).not.toHaveProperty('items');
});

test('polls active parsing/extraction at five seconds, pauses hidden, stops terminal and unmounted', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  records = [document]; const view = mount();
  const advance = async (ms = 20) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
  await advance(); await vi.waitFor(() => { expect(reads).toBe(1); });
  await advance(4900); expect(reads).toBe(1); await advance(200); expect(reads).toBe(2);
  act(() => { focusManager.setFocused(false); }); await advance(15000); expect(reads).toBe(2);
  state = { ...document, status: 'processing' }; act(() => { focusManager.setFocused(true); }); await advance(5000); expect(reads).toBe(3);
  state = { ...document, status: 'ready' }; await advance(5000); await advance(); expect(extractions).toBe(1);
  proposals = { status: 'running', source_version: 1 }; await advance(5000); expect(extractions).toBe(2);
  proposals = { status: 'ready', count: 2, language: 'fr', source_version: 1 }; await advance(5000); await advance();
  expect(screen.getByText(/2 candidates · Language: fr/)).toBeVisible(); const completedReads = reads;
  await advance(20000); expect(reads).toBe(completedReads); expect(extractions).toBe(3);
  view.unmount(); await advance(20000); expect(reads).toBe(completedReads); expect(extractions).toBe(3);
});

test('retries failed list and detail/extraction reads without treating failures as empty successes', async () => {
  const user = userEvent.setup(); server.use(http.get(base, () => failure(503))); mount();
  expect(await screen.findByRole('alert')).toHaveTextContent('Documents could not be loaded');
  records = [{ ...document, status: 'ready' }]; state = records[0] as Document;
  server.use(http.get(base, () => HttpResponse.json(records)), http.get(`${base}/${document.id}`, () => failure(503)), http.get(`${base}/${document.id}/proposals`, () => failure(503)));
  await user.click(screen.getByRole('button', { name: 'Refresh documents' })); expect(await screen.findByText(/Status may be out of date/)).toBeVisible();
  server.use(http.get(`${base}/${document.id}`, () => HttpResponse.json(state)));
  await user.click(screen.getByRole('button', { name: 'Retry status' }));
  await user.click(await screen.findByRole('button', { name: 'Retry extraction status' }));
  server.use(http.get(`${base}/${document.id}/proposals`, () => HttpResponse.json({ status: 'ready', source_version: 1 })));
  await user.click(await screen.findByRole('button', { name: 'Retry extraction status' })); expect(await screen.findByText(/Extraction complete/)).toBeVisible();
});

test.each(['unmount', 'owner change'])('aborts an active status request and its timer on %s', async action => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  const pending = gate(); let signal: AbortSignal | undefined;
  records = [document]; server.use(http.get(`${base}/${document.id}`, async ({ request }) => {
    reads++; signal = request.signal; await pending.pending; return HttpResponse.json(document);
  }));
  const view = mount(); await act(async () => { await vi.advanceTimersByTimeAsync(50); });
  expect(reads).toBe(1); expect(signal?.aborted).toBe(false);
  if (action === 'unmount') view.unmount();
  else { records = []; act(() => { api.acceptSession({ ...owner, id: 'next-owner' }); }); view.rerender(tree('next-owner')); }
  expect(signal?.aborted).toBe(true); pending.release();
  await act(async () => { await vi.advanceTimersByTimeAsync(20000); }); expect(reads).toBe(1);
  expect(screen.queryByText(document.filename)).not.toBeInTheDocument();
});

test('polls deletion through cleanup completion and stops polling request failures', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  const advance = async (ms = 50) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
  records = [document]; server.use(http.delete(`${base}/${document.id}`, () => {
    state = { id: document.id, status: 'deleting' }; records = []; return HttpResponse.json(state, { status: 202 });
  }));
  mount(); await advance(); fireEvent.click(screen.getByRole('button', { name: `Delete ${document.filename}` }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm deletion' })); await advance(); expect(screen.getByText('Deleting')).toBeVisible();
  server.use(http.get(`${base}/${document.id}`, () => { reads++; return failure(503); }));
  await advance(5000); await advance(); const failedReads = reads; await advance(20000); expect(reads).toBe(failedReads);
  server.use(http.get(`${base}/${document.id}`, () => { reads++; return HttpResponse.json({ id: document.id, status: 'deleted' }); }));
  fireEvent.click(screen.getByRole('button', { name: 'Retry status' })); await advance(); expect(screen.getByText('Deleted')).toBeVisible();
  await advance(20000); expect(reads).toBe(failedReads + 1);
});

test('confirms deletion, retries failure and cleanup, preserves facts while removing cached evidence, then dismisses', async () => {
  const user = userEvent.setup(); records = [document]; let calls = 0;
  const snapshot: components['schemas']['ProfileSnapshot'] = { id: owner.id, revision: 1, facts: [{ id: crypto.randomUUID(), data: { kind: 'skill', name: 'Python' }, created_at: document.created_at, updated_at: document.created_at,
    source_associations: [{ document_id: document.id, source_version: 1, language: null, field: 'name', quote: 'Private quote', locator: 'Line 1', proposal_id: crypto.randomUUID() }] }] };
  api.queryClient.setQueryData(['profile', owner.id], snapshot);
  server.use(http.delete(`${base}/${document.id}`, ({ request }) => {
    expect(request.headers.get('x-csrf-token')).toBe(owner.csrf_token);
    if (++calls === 1) return failure(503);
    records = []; state = { id: document.id, status: 'deleting', error: 'cleanup_failed' }; return HttpResponse.json(state, { status: 202 });
  }));
  mount(); const remove = await screen.findByRole('button', { name: `Delete ${document.filename}` }); await user.click(remove);
  expect(screen.getByRole('button', { name: 'Cancel deletion' })).toHaveFocus(); await user.keyboard('{Enter}'); expect(calls).toBe(0);
  await user.click(screen.getByRole('button', { name: `Delete ${document.filename}` })); await user.click(screen.getByRole('button', { name: 'Confirm deletion' }));
  expect(await screen.findByText(/Deletion could not be confirmed/)).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Confirm deletion' })); expect(await screen.findByText(/Cleanup failed/)).toBeVisible();
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
  const cached = api.queryClient.getQueryData<components['schemas']['ProfileSnapshot']>(['profile', owner.id]);
  expect(cached?.facts?.[0]).toMatchObject({ data: { kind: 'skill', name: 'Python' }, source_associations: [], unavailable_sources: [document.id] });
  server.use(http.delete(`${base}/${document.id}`, () => { state = { id: document.id, status: 'deleted' }; return HttpResponse.json(state); }));
  await user.click(screen.getByRole('button', { name: 'Retry deletion' })); await user.click(await screen.findByRole('button', { name: 'Dismiss deleted document' }));
  expect(await screen.findByText(/No documents yet/)).toBeVisible();
});

test('aborts an upload and private polling on account change; late replies cannot repopulate old data', async () => {
  const user = userEvent.setup(), pending = gate(); let requested = false;
  records = [document]; server.use(http.post(base, async () => { requested = true; await pending.pending; return HttpResponse.json(document, { status: 201 }); }));
  const view = mount(); await user.upload(picker(), new File(['a'], 'old.txt')); await user.click(uploadButton()); await waitFor(() => { expect(requested).toBe(true); });
  records = []; act(() => { api.acceptSession({ ...owner, id: 'other-owner', csrf_token: 'd'.repeat(64) }); }); view.rerender(tree('other-owner'));
  pending.release(); await screen.findByText(/No documents yet/);
  expect(screen.queryByText(/Selected:|Document uploaded|résumé.txt/)).not.toBeInTheDocument();
  expect(api.queryClient.getQueriesData({ queryKey: ['documents', owner.id] })).toEqual([]);
});

test('refreshes the session when documents become unauthorized', async () => {
  const invalidate = vi.spyOn(api, 'invalidateSession').mockResolvedValue(); server.use(http.get(base, () => failure(403))); mount();
  await waitFor(() => { expect(invalidate).toHaveBeenCalled(); });
});

test('paginates at the server page size and returns from an empty later page', async () => {
  records = Array.from({ length: 100 }, (_, index) => ({ ...document, id: String(index), status: 'unsupported' }));
  server.use(http.get(base, ({ request }) => HttpResponse.json(new URL(request.url).searchParams.get('offset') === '0' ? records : [])),
    http.get(`${base}/:id`, ({ params }) => HttpResponse.json({ ...document, id: params.id, status: 'unsupported' })));
  const user = userEvent.setup(); mount(); await user.click(await screen.findByRole('button', { name: 'Next documents' }));
  expect(await screen.findByText(/No documents on this page/)).toBeVisible(); await user.click(screen.getByRole('button', { name: 'Previous documents' }));
  await waitFor(() => { expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(100); });
});
