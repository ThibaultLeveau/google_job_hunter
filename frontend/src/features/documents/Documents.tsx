import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError } from '../../shared/api';
import { Badge, Button } from '../../shared/ui';
import { isProcessing, processingHelp, processingStates } from '../../shared/processing';
import { ProposalReview } from '../profile/ProposalReview';

type Document = components['schemas']['Document'];
type Deletion = components['schemas']['DeletionState'];
type Proposals = components['schemas']['ProposalPage'];
type Snapshot = components['schemas']['ProfileSnapshot'];
const root = '/api/v1/documents';
const mediaTypes: Record<string, string> = { pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', txt: 'text/plain' };
const labels = { queued: 'Waiting to parse', processing: 'Parsing', ready: 'Parsed', failed: 'Parsing failed', unsupported: processingStates.unsupported, deleting: 'Deleting', deleted: 'Deleted' };
const extractionLabels = { waiting_for_parse: 'Waiting to parse', pending: 'Waiting for extraction', running: 'Extracting facts', failed: 'Extraction failed. Try a corrected document or add facts manually.', unavailable: 'Extraction unavailable. You can add facts manually.', ready: 'Extraction complete' };
const parseHelp: Record<NonNullable<Document['parse_error']>, string> = {
  ocr_required: 'This scanned PDF needs OCR. Upload a text-based PDF, DOCX or TXT; OCR is not available here.',
  encrypted_document: 'This document is password-protected. Upload an unlocked copy.',
  empty_document: 'No readable text was found. Upload a document containing text.',
  limits_exceeded: 'This document exceeds parsing limits. Upload a shorter, simpler document.',
  timed_out: processingHelp('timed_out', 'Parsing'),
  cancelled: 'Parsing was cancelled. Upload a different document or add facts manually.',
  storage_unavailable: 'Document storage is unavailable. Contact your workspace administrator.',
  unsupported_format: 'This format cannot be parsed. Upload PDF, DOCX or UTF-8 TXT.',
  invalid_document: 'This document could not be read. Upload a corrected copy.',
};

function useRequestLifetime() {
  const controller = useRef(new AbortController());
  useEffect(() => { controller.current = new AbortController(); return () => { controller.current.abort(); }; }, []);
  return controller;
}
function useSessionRecovery(...errors: unknown[]) {
  const expired = errors.some(error => error instanceof ApiError && (error.status === 401 || error.status === 403));
  useEffect(() => { if (expired) void api.invalidateSession(); }, [expired]);
}
function uploadError(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 413) return 'This file exceeds your workspace upload limit. Choose a smaller file.';
    if (error.status === 415) return 'The file content is not supported. Choose a valid PDF, DOCX or UTF-8 TXT.';
    if (error.status === 409) return 'Your document quota was reached, or these exact bytes were previously deleted. Contact your workspace administrator.';
  }
  return 'Upload could not be confirmed. Your selection is kept. Try again; identical bytes reuse the existing document.';
}

function DocumentRow({ document: original, ownerId, retain, dismiss }: {
  document: Document; ownerId: string; retain: (document: Document) => void; dismiss: () => void;
}) {
  const prefix = ['documents', ownerId];
  const key = [...prefix, 'detail', original.id];
  const extractionKey = [...prefix, 'extraction', original.id];
  const path = `${root}/${encodeURIComponent(original.id)}` as const;
  const request = useRequestLifetime();
  const detail = useQuery<Document | Deletion>({ queryKey: key, initialData: original,
    queryFn: ({ signal }) => api.request(path, { signal }), retry: false, refetchOnWindowFocus: false,
    refetchInterval: query => !query.state.error && isProcessing(query.state.data?.status ?? '')
      && !('error' in (query.state.data ?? {}) && (query.state.data as Deletion).error) ? 5000 : false,
    refetchIntervalInBackground: false,
  });
  const document = detail.data;
  const removed = document.status === 'deleting' || document.status === 'deleted';
  const extraction = useQuery({ queryKey: extractionKey, enabled: document.status === 'ready' && !detail.isError,
    queryFn: async ({ signal }) => {
      const result = await api.request<Proposals>(`${path}/proposals`, { signal });
      return { status: result.status, count: result.count ?? 0, language: result.language ?? 'Unknown' };
    }, retry: false, refetchOnWindowFocus: false, refetchIntervalInBackground: false,
    refetchInterval: query => !query.state.error && isProcessing(query.state.data?.status ?? '') ? 5000 : false,
  });
  const [confirming, setConfirming] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const busy = useRef(false);
  useEffect(() => { if (confirming) cancel.current?.focus(); }, [confirming]);
  const deletion = useMutation({
    mutationFn: () => api.request<Deletion>(path, { method: 'DELETE', signal: request.current.signal }),
    onSuccess: result => {
      retain(original); setConfirming(false); heading.current?.focus();
      void api.queryClient.cancelQueries({ queryKey: key });
      void api.queryClient.cancelQueries({ queryKey: extractionKey });
      api.queryClient.removeQueries({ queryKey: extractionKey });
      const reviewKey = [...prefix, 'review', original.id];
      void api.queryClient.cancelQueries({ queryKey: reviewKey });
      api.queryClient.removeQueries({ queryKey: reviewKey });
      api.queryClient.setQueryData(key, result);
      // Remove deleted evidence immediately even if refreshing the profile fails.
      const profileKey = ['profile', ownerId];
      void api.queryClient.cancelQueries({ queryKey: profileKey });
      api.queryClient.setQueryData<Snapshot>(profileKey, snapshot => snapshot && ({ ...snapshot,
        facts: (snapshot.facts ?? []).map(fact => ({ ...fact,
          source_associations: (fact.source_associations ?? []).filter(source => source.document_id !== original.id),
          unavailable_sources: [...new Set([...(fact.unavailable_sources ?? []),
            ...((fact.source_associations ?? []).some(source => source.document_id === original.id) ? [original.id] : [])])],
        })),
      }));
      void api.queryClient.invalidateQueries({ queryKey: profileKey });
      void api.queryClient.invalidateQueries({ queryKey: [...prefix, 'list'] });
    },
    onSettled: () => { busy.current = false; },
  });
  useSessionRecovery(detail.error, extraction.error, deletion.error);
  function remove() { if (!busy.current) { busy.current = true; deletion.mutate(); } }
  return <li>
    <h3 ref={heading} tabIndex={-1}>{original.filename}</h3>
    <p>{(original.size_bytes / 1024).toFixed(1)} KiB · Version {original.version}</p>
    <p role="status"><Badge>{labels[document.status]}</Badge></p>
    {detail.isError && <p role="alert">Status may be out of date. <Button tone="secondary" loading={detail.isFetching}
      onClick={() => { void detail.refetch(); }}>Retry status</Button></p>}
    {'parse_error' in document && document.parse_error && <p>{parseHelp[document.parse_error]}</p>}
    {document.status === 'ready' && <div role="status">
      {extraction.isError ? <p>Extraction status could not be loaded. <Button tone="secondary" loading={extraction.isFetching}
        onClick={() => { void extraction.refetch(); }}>Retry extraction status</Button></p>
        : extraction.data ? <p>{extractionLabels[extraction.data.status]}
          {extraction.data.status === 'ready' && ` · ${String(extraction.data.count)} candidates · Language: ${extraction.data.language}. Accepted facts change only after review.`}</p>
          : <p>Loading extraction status…</p>}
    </div>}
    {!removed && extraction.data?.status === 'ready' && <>
      <Button tone="secondary" aria-expanded={reviewing} onClick={() => { setReviewing(previous => !previous); }}>
        {reviewing ? 'Close review' : 'Review candidates'}</Button>
      {reviewing && <ProposalReview ownerId={ownerId} documentId={original.id} filename={original.filename} />}
    </>}
    {deletion.isError && <p role="alert">Deletion could not be confirmed. Try again to check or resume deletion.</p>}
    {removed ? <>
      <p>Your accepted facts are kept; evidence from this document is unavailable.</p>
      {'error' in document && document.error && <p role="alert">Cleanup failed. <Button loading={deletion.isPending}
        onClick={remove}>Retry deletion</Button></p>}
      {document.status === 'deleted' && <Button tone="secondary" onClick={dismiss}>Dismiss deleted document</Button>}
    </> : confirming ? <div className="document-confirm" role="group" aria-label={`Confirm deletion of ${original.filename}`}>
      <p>Delete this document and its extracted data? Accepted facts are kept, but its source evidence becomes unavailable.</p>
      <div className="profile-editor-actions">
        <Button ref={cancel} tone="secondary" disabled={deletion.isPending} onClick={() => { setConfirming(false); heading.current?.focus(); }}>Cancel deletion</Button>
        <Button tone="danger" loading={deletion.isPending} loadingLabel="Deleting…" onClick={remove}>Confirm deletion</Button>
      </div>
    </div> : <div className="profile-editor-actions">
      <a href={`${path}/download`}>Download {original.filename}</a>
      <Button tone="danger" onClick={() => { setConfirming(true); }}>Delete {original.filename}</Button>
    </div>}
  </li>;
}

export function Documents({ ownerId }: { ownerId: string }) {
  const [offset, setOffset] = useState(0);
  const [retained, setRetained] = useState<Document[]>([]);
  const [selected, setSelected] = useState<File | null>(null);
  const [invalid, setInvalid] = useState('');
  const [notice, setNotice] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const busy = useRef(false);
  const request = useRequestLifetime();
  const prefix = ['documents', ownerId];
  const list = useQuery({ queryKey: [...prefix, 'list', offset], retry: false,
    queryFn: ({ signal }) => api.request<Document[]>(`${root}?offset=${String(offset)}`, { signal }),
  });
  function retain(document: Document) { setRetained(previous => [...previous.filter(item => item.id !== document.id), document]); }
  const upload = useMutation({
    mutationFn: ({ file, mediaType }: { file: File; mediaType: string }) => api.request<Document>(root,
      { method: 'POST', upload: { file, mediaType }, signal: request.current.signal, withStatus: true }),
    onSuccess: ({ data, status }) => {
      retain(data); setSelected(null); if (input.current) input.current.value = '';
      setNotice(status === 200 ? 'This document already exists. Its original name and processing state are kept.' : 'Document uploaded. Processing will continue in the background.');
      api.queryClient.setQueryData([...prefix, 'detail', data.id], data);
      void api.queryClient.invalidateQueries({ queryKey: prefix });
    },
    onSettled: () => { busy.current = false; },
  });
  useSessionRecovery(list.error, upload.error);
  function select(files: FileList | null) {
    if (busy.current) return;
    upload.reset(); setNotice(''); setSelected(null);
    const file = files?.[0];
    if (!file) { setInvalid(''); return; }
    if (files.length !== 1) { setInvalid('Choose one document at a time.'); return; }
    const type = mediaTypes[file.name.split('.').at(-1)?.toLowerCase() ?? ''];
    if (!type || file.size === 0 || file.size > 50 * 1024 * 1024) {
      setInvalid('Choose a non-empty PDF, DOCX or TXT file, no larger than 50 MiB. Your workspace may have a lower limit.'); return;
    }
    setInvalid(''); setSelected(file);
  }
  const documents = [...new Map([...(list.data ?? []), ...retained].map(document => [document.id, document])).values()];
  return <section aria-labelledby="documents-heading" className="documents">
    <h2 id="documents-heading" tabIndex={-1}>Documents</h2>
    <form className="document-upload" noValidate onSubmit={event => {
      event.preventDefault();
      if (!selected) { setInvalid('Choose a document to upload.'); input.current?.focus(); return; }
      if (busy.current) return;
      busy.current = true;
      upload.mutate({ file: selected, mediaType: mediaTypes[selected.name.split('.').at(-1)?.toLowerCase() ?? ''] ?? 'application/octet-stream' });
    }} onDragOver={event => { event.preventDefault(); }} onDrop={event => { event.preventDefault(); select(event.dataTransfer.files); }}>
      <label htmlFor="document-file">Choose a document, or drop one here</label>
      <p id="document-help">PDF, DOCX or UTF-8 TXT. The upload limit is 10 MiB by default; your workspace may use a different limit.</p>
      <input id="document-file" ref={input} type="file" accept=".pdf,.docx,.txt" disabled={upload.isPending}
        aria-describedby={`document-help${invalid || upload.isError ? ' document-error' : ''}`} aria-invalid={Boolean(invalid || upload.isError)}
        onChange={event => { select(event.target.files); }} />
      {selected && <p>Selected: {selected.name}</p>}
      {(invalid || upload.isError) && <p id="document-error" role="alert">{invalid || uploadError(upload.error)}</p>}
      <Button type="submit" loading={upload.isPending} loadingLabel="Uploading…">{upload.isError ? 'Retry upload' : 'Upload document'}</Button>
      {upload.isPending && <p role="status">Uploading your document. Progress is not measurable.</p>}
    </form>
    <p role="status" aria-atomic="true">{notice}</p>
    <Button tone="secondary" loading={list.isFetching} onClick={() => { void api.queryClient.invalidateQueries({ queryKey: prefix }); }}>Refresh documents</Button>
    {list.isError && <p role="alert">Documents could not be loaded. Any displayed results may be out of date. Refresh to try again.</p>}
    {list.isPending && <p role="status">Loading documents…</p>}
    {list.isSuccess && !documents.length && <p>{offset ? 'No documents on this page. Go back to the previous page.' : 'No documents yet. Upload a document to extract profile candidates.'}</p>}
    <ul className="profile-facts">{documents.map(document => <DocumentRow key={document.id} document={document} ownerId={ownerId}
      retain={retain} dismiss={() => {
        setRetained(previous => previous.filter(item => item.id !== document.id));
        window.document.getElementById('documents-heading')?.focus();
      }} />)}</ul>
    <div className="profile-editor-actions" aria-label="Document pages">
      <Button tone="secondary" disabled={!offset || list.isFetching} onClick={() => { setOffset(previous => Math.max(0, previous - 100)); }}>Previous documents</Button>
      <Button tone="secondary" disabled={list.isFetching || list.isError || list.data?.length !== 100} onClick={() => { setOffset(previous => previous + 100); }}>Next documents</Button>
    </div>
  </section>;
}
