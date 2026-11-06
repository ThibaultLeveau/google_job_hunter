import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError } from '../../shared/api';
import { Badge, Button } from '../../shared/ui';
import { FactEditor, type EditorTarget } from './FactEditor';

type Proposal = components['schemas']['Proposal'];
type Page = components['schemas']['ProposalPage'];
type Snapshot = components['schemas']['ProfileSnapshot'];
type Fact = components['schemas']['ProfileFact']['data'];
type Batch = components['schemas']['ReviewBatch'];
type Result = components['schemas']['ReviewResult'];
type Outcome = components['schemas']['ReviewOutcome']['outcome'];
const labels = { name: 'Name', company: 'Company', title: 'Job title', start_date: 'Start date', end_date: 'End date', ongoing: 'Ongoing', description: 'Description', url: 'Project link' };
const outcomes = { created: 'Accepted: fact created', linked: 'Accepted: linked to an existing fact', rejected: 'Rejected', conflict: 'Conflict: not applied. Check values and existing facts before retrying; an earlier decision may already exist.' };
const states = { waiting_for_parse: 'Waiting for parsing.', pending: 'Waiting for extraction.', running: 'Extraction is running.', failed: 'Extraction failed. Try a corrected document or add facts manually.', unavailable: 'Extraction is unavailable. You can add facts manually.', ready: '' };
const display = (value: unknown) => typeof value === 'string' ? value : value === true ? 'Yes' : value === false ? 'No' : 'Unknown';
function initial(proposal: Proposal): Fact {
  const value = (name: string) => proposal.candidate.fields.find(field => field.name === name)?.value;
  const text = (name: string) => { const result = value(name); return typeof result === 'string' ? result : null; };
  if (proposal.candidate.kind === 'skill') return { kind: 'skill', name: text('name') ?? '' };
  const dates = { start_date: text('start_date'), end_date: text('end_date'), description: text('description'),
    ongoing: typeof value('ongoing') === 'boolean' ? value('ongoing') as boolean : null };
  return proposal.candidate.kind === 'experience'
    ? { kind: 'experience', company: text('company') ?? '', title: text('title') ?? '', ...dates }
    : { kind: 'project', name: text('name') ?? '', url: text('url'), ...dates };
}
function Values({ data }: { data: Fact }) {
  return <dl>{Object.entries(data).filter(([key]) => key !== 'kind').map(([key, value]) => <div key={key}>
    <dt>{labels[key as keyof typeof labels]}</dt><dd>{display(value)}</dd>
  </div>)}</dl>;
}
function Evidence({ proposal }: { proposal: Proposal }) {
  return <dl className="proposal-evidence">{proposal.candidate.fields.map(field => <div key={field.name}>
    <dt>{labels[field.name]} <Badge>{field.uncertainty === 'explicit' ? 'Source value' : field.uncertainty === 'uncertain' ? 'Uncertain' : 'Missing'}</Badge></dt>
    <dd>{display(field.value)}{field.evidence.map((span, index) => <figure key={index}>
      <blockquote>{span.quote}</blockquote><figcaption>{span.locator} · Characters {span.start}–{span.end}</figcaption>
    </figure>)}</dd>
  </div>)}</dl>;
}
function suggestions(proposal: Proposal, facts: Snapshot['facts']) {
  const proposed = initial(proposal);
  const keys = proposed.kind === 'skill' ? ['name'] : proposed.kind === 'experience'
    ? ['company', 'title', 'start_date', 'end_date', 'ongoing'] : ['name', 'url', 'start_date', 'end_date', 'ongoing'];
  const comparable = (key: string, value: unknown) => typeof value === 'string'
    ? key === 'url' ? value.trim() : value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLowerCase() : value ?? null;
  return (facts ?? []).filter(fact => fact.data.kind === proposed.kind && keys.every(key =>
    comparable(key, Object.entries(fact.data).find(([name]) => name === key)?.[1])
      === comparable(key, Object.entries(proposed).find(([name]) => name === key)?.[1])));
}

export function ProposalReview({ ownerId, documentId, filename }: { ownerId: string; documentId: string; filename: string }) {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [drafts, setDrafts] = useState<Record<string, Fact>>({});
  const [decisions, setDecisions] = useState<Record<string, Outcome>>({});
  const [batch, setBatch] = useState<Batch | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [editor, setEditor] = useState<{ target: EditorTarget; proposal: Proposal } | null>(null);
  const request = useRef(new AbortController()), busy = useRef(false);
  useEffect(() => { request.current = new AbortController(); return () => { request.current.abort(); }; }, []);
  const profile = useQuery({ queryKey: ['profile', ownerId], retry: false,
    queryFn: ({ signal }) => api.request<Snapshot>('/api/v1/profile', { signal }),
  });
  const proposals = useQuery({ queryKey: ['documents', ownerId, 'review', documentId, offset], retry: false,
    queryFn: ({ signal }) => api.request<Page>(`/api/v1/documents/${encodeURIComponent(documentId)}/proposals?offset=${String(offset)}`, { signal }),
  });
  const mutation = useMutation({ mutationFn: (body: Batch) => api.request<Result>('/api/v1/profile/review',
    { method: 'POST', body, signal: request.current.signal }),
    onSuccess: data => {
      setResult(data); setBatch(null);
      setDecisions(previous => ({ ...previous, ...Object.fromEntries(data.items.map(item => [item.proposal_id, item.outcome])) }));
      setSelected(previous => new Set([...previous].filter(id => !data.items.some(item => item.proposal_id === id && item.outcome !== 'conflict'))));
      void api.queryClient.invalidateQueries({ queryKey: ['profile', ownerId] });
    }, onSettled: () => { busy.current = false; },
  });
  const errors = [profile.error, proposals.error, mutation.error];
  const expired = errors.some(error => error instanceof ApiError && [401, 403].includes(error.status));
  const unavailable = errors.some(error => error instanceof ApiError && error.status === 404);
  useEffect(() => { if (expired) void api.invalidateSession(); }, [expired]);
  const conflict = mutation.error instanceof ApiError && [409, 422].includes(mutation.error.status);
  const blocked = !profile.data || profile.isFetching || profile.isError || proposals.isFetching || proposals.isError || Boolean(batch);
  function send(body: Batch) {
    if (busy.current) return;
    busy.current = true; setBatch(body); mutation.mutate(body);
  }
  function submit(action: 'accept' | 'reject') {
    if (blocked || !selected.size) return;
    send({ expected_revision: profile.data.revision, idempotency_key: crypto.randomUUID(), items: [...selected].map(id =>
      action === 'reject' ? { proposal_id: id, action } : drafts[id]
        ? { proposal_id: id, action: 'edit', fact: drafts[id] } : { proposal_id: id, action }) });
  }
  async function reload() {
    const [latest, candidates] = await Promise.all([profile.refetch(), proposals.refetch()]);
    if (latest.isSuccess && candidates.isSuccess && conflict) { mutation.reset(); setBatch(null); }
  }
  if (unavailable) return <p role="alert">This document or a selected candidate is unavailable. Close this review and refresh the document list.</p>;
  return <section className="proposal-review" aria-label={`Review candidates from ${filename}`}>
    <h4>Proposed facts from {filename}</h4>
    <p>Review source evidence before accepting. Proposals and draft edits are separate from your accepted profile.</p>
    {profile.isPending && <p role="status">Loading accepted profile…</p>}
    <Button tone="secondary" loading={profile.isFetching || proposals.isFetching} disabled={mutation.isPending}
      onClick={() => { void reload(); }}>Reload profile and candidates</Button>
    {(profile.isError || proposals.isError) && <p role="alert">Review data could not be loaded. Your selections and drafts are kept; reload before continuing.</p>}
    {mutation.isError && <p role="alert">{conflict ? 'The profile revision changed or a value was rejected. Reload, review your kept choices, then submit again.'
      : 'The result could not be confirmed. Retry the same decisions to safely recover their result.'}
      {!conflict && batch && <Button onClick={() => { send(batch); }} loading={mutation.isPending}>Retry same decisions</Button>}</p>}
    <p role="status">{result && (['created', 'linked', 'rejected', 'conflict'] as const).map(outcome =>
      `${String(result.items.filter(item => item.outcome === outcome).length)} ${outcome}`).join(' · ')}</p>
    {!proposals.data && proposals.isPending && <p role="status">Loading candidates…</p>}
    {proposals.data && <>
      <p>Source version {proposals.data.source_version} · Language: {proposals.data.language ?? 'Unknown'}</p>
      {proposals.data.status !== 'ready' ? <p>{states[proposals.data.status]}</p> : <>
        {!proposals.data.items?.length && <p>No candidates on this page. You can add facts manually.</p>}
        <p>{selected.size} selected across pages (maximum 100 per submission).</p>
        <div className="profile-editor-actions">
          <Button disabled={blocked || !selected.size} loading={mutation.isPending} onClick={() => { submit('accept'); }}>Accept selected</Button>
          <Button tone="secondary" disabled={blocked || !selected.size} onClick={() => { submit('reject'); }}>Reject selected</Button>
        </div>
        <ul className="profile-facts">{(proposals.data.items ?? []).map(proposal => {
          const decision = decisions[proposal.id];
          const completed = Boolean(decision && decision !== 'conflict');
          const draft = drafts[proposal.id];
          const existing = suggestions(proposal, profile.data?.facts);
          const title = proposal.candidate.fields.find(field => field.name === 'name' || field.name === 'title')?.value;
          const disabled = blocked || completed || (!selected.has(proposal.id) && selected.size >= 100);
          return <li key={proposal.id}>
            <h5>Proposed {proposal.candidate.kind}: {display(title)}</h5>
            {decision && <p role="status">{outcomes[decision]}</p>}
            <label className="proposal-choice"><input type="checkbox" checked={selected.has(proposal.id)} disabled={disabled}
              onChange={event => { const checked = event.target.checked; setSelected(previous => {
                const next = new Set(previous); if (checked) next.add(proposal.id); else next.delete(proposal.id); return next;
              }); }} />Select proposed {proposal.candidate.kind}: {display(title)}</label>
            <Evidence proposal={proposal} />
            {existing.length > 0 && <details><summary>Existing facts with matching identifying values ({existing.length})</summary>
              <p>Check all values. The server decides whether to link or report a conflict; accepted facts are never overwritten here.</p>
              {existing.map(fact => <Values key={fact.id} data={fact.data} />)}
            </details>}
            {draft && <div><h5>Your draft correction</h5><Values data={draft} /></div>}
            <Button tone="secondary" disabled={disabled} onClick={event => { setEditor({ proposal, target: {
              kind: proposal.candidate.kind, fact: null, initial: drafts[proposal.id] ?? initial(proposal), remove: false,
              revision: profile.data?.revision ?? 0, opener: event.currentTarget,
            } }); }}>Edit proposed {proposal.candidate.kind}</Button>
          </li>;
        })}</ul>
        <div className="profile-editor-actions">
          <Button tone="secondary" disabled={!offset || proposals.isFetching} onClick={() => { setOffset(previous => Math.max(0, previous - 100)); }}>Previous candidates</Button>
          <Button tone="secondary" disabled={proposals.isFetching || offset + 100 >= (proposals.data.count ?? 0)} onClick={() => { setOffset(previous => previous + 100); }}>Next candidates</Button>
        </div>
      </>}
    </>}
    {editor && <FactEditor target={editor.target} missing={false} evidence={<Evidence proposal={editor.proposal} />}
      reload={async () => { const latest = await profile.refetch(); if (!latest.isSuccess) throw new Error('Profile unavailable'); return latest.data; }}
      saved={() => Promise.resolve()} saveDraft={data => {
        setDrafts(previous => ({ ...previous, [editor.proposal.id]: data }));
        setSelected(previous => new Set([...previous, editor.proposal.id]));
      }} close={() => { const opener = editor.target.opener; setEditor(null); opener.focus(); }} />}
  </section>;
}
