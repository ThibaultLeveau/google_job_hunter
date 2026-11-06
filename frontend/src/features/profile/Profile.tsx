import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError } from '../../shared/api';
import { Badge, Button, Field, StatePanel } from '../../shared/ui';
import { FactEditor, type EditorTarget } from './FactEditor';
import { Documents } from '../documents/Documents';

type Snapshot = components['schemas']['ProfileSnapshot'];
type Fact = components['schemas']['ProfileFact'];
type Write = components['schemas']['FactWrite'];
type Result = components['schemas']['FactResult'];
type Action = { revision: number } & ({ kind: 'add'; name: string } | { kind: 'remove'; id: string });

function Evidence({ fact }: { fact: Fact }) {
  const sources = fact.source_associations ?? [];
  const unavailable = fact.unavailable_sources ?? [];
  return <div className="profile-evidence">
    <Badge>{sources.length ? 'Source-supported' : unavailable.length ? 'Source unavailable' : 'Manual'}</Badge>
    {sources.length > 0 && (fact.user_fields ?? []).length > 0 && <Badge>Includes manual edits</Badge>}
    {unavailable.length > 0 && <p>Evidence from a deleted document is unavailable. Your accepted fact is kept.</p>}
    {sources.length > 0 && <details><summary>View source evidence ({sources.length})</summary>
      <ul>{sources.map((source, index) => <li key={index}>
        <p>{source.locator} · Language: {source.language ?? 'Unknown'}</p>
        <blockquote>{source.quote}</blockquote>
        <a href={`/api/v1/documents/${encodeURIComponent(source.document_id)}/download`}>
          Download source document (version {source.source_version})
        </a>
      </li>)}</ul>
    </details>}
  </div>;
}

function FactDetails({ fact }: { fact: Fact }) {
  const data = fact.data;
  if (data.kind === 'skill') return <h3>{data.name}</h3>;
  if (data.kind !== 'experience' && data.kind !== 'project') return <p>Unsupported profile fact.</p>;
  return <>
    <h3>{data.kind === 'experience' && `${data.title} · ${data.company}`}{data.kind === 'project' && data.name}</h3>
    <p>{data.start_date ?? 'Start date unknown'} — {data.end_date ?? (data.ongoing ? 'Present' : 'End date unknown')}
      {data.ongoing == null && ' · Ongoing status unknown'}</p>
    <p className="profile-description">{data.description ?? 'Description not provided.'}</p>
    {data.kind === 'project' && <p>{data.url
      ? /^https?:\/\/[^\s]+$/i.test(data.url) ? <a href={data.url} rel="noreferrer">{data.url}</a> : data.url
      : 'Project link not provided.'}</p>}
  </>;
}

export function Profile({ ownerId }: { ownerId: string }) {
  const queryKey = ['profile', ownerId] as const;
  const profile = useQuery({ queryKey,
    queryFn: ({ signal }) => api.request<Snapshot>('/api/v1/profile', { signal }),
  });
  const [name, setName] = useState('');
  const [invalid, setInvalid] = useState(false);
  const [notice, setNotice] = useState('');
  const [editor, setEditor] = useState<EditorTarget | null>(null);
  const restoreFocus = useRef<EditorTarget | null>(null);
  useLayoutEffect(() => {
    const previous = restoreFocus.current;
    if (editor || !previous) return;
    restoreFocus.current = null;
    const target = previous.opener.isConnected && !previous.opener.matches(':disabled')
      ? previous.opener : document.getElementById(`profile-add-${previous.kind}`);
    if (target && !target.matches(':disabled')) target.focus();
    else document.getElementById(`profile-${previous.kind}`)?.focus();
  }, [editor]);
  const input = useRef<HTMLInputElement>(null);
  const busy = useRef(false);
  const mutation = useMutation({
    mutationFn: (action: Action) => {
      if (action.kind === 'remove') return api.request<Result>(
        `/api/v1/profile/facts/${encodeURIComponent(action.id)}?expected_revision=${String(action.revision)}`, { method: 'DELETE' });
      const body: Write = { expected_revision: action.revision, fact: { kind: 'skill', name: action.name } };
      return api.request<Result>('/api/v1/profile/facts', { method: 'POST', body });
    },
    onSuccess: async (result, action) => {
      setNotice(result.outcome === 'existing' ? 'That skill is already in your profile. Your original wording was kept.'
        : action.kind === 'add' ? 'Skill added.' : 'Skill removed.');
      if (action.kind === 'add') setName('');
      await api.queryClient.invalidateQueries({ queryKey, exact: true });
      input.current?.focus();
    },
    onSettled: () => { busy.current = false; },
  });
  const error = mutation.error;
  const conflict = error instanceof ApiError && (error.status === 409 || error.status === 404);
  const fieldError = invalid || (error instanceof ApiError && error.status === 422)
    ? 'Enter a skill name of 1–120 characters.' : undefined;
  const inaccessible = [profile.error, error].some(value => value instanceof ApiError && (value.status === 401 || value.status === 403));
  useEffect(() => { if (inaccessible) void api.invalidateSession(); }, [inaccessible]);

  const blocked = mutation.isPending || profile.isFetching || profile.isError || conflict;
  function run(action: Action) {
    if (busy.current || blocked) return;
    busy.current = true; setNotice(''); mutation.mutate(action);
  }
  async function reload() {
    const result = await profile.refetch();
    if (result.isSuccess) {
      mutation.reset(); setNotice('Profile reloaded. Review your changes before trying again.'); input.current?.focus();
    }
  }
  if (!profile.data) return <StatePanel state={profile.isError ? 'error' : 'loading'}
    title={profile.isError ? 'Your profile could not be loaded' : 'Loading your profile'}
    description={profile.isError ? 'Try again to see your accepted facts.' : 'Getting your accepted skills, experience and projects.'}
    action={profile.isError && <Button loading={profile.isFetching} onClick={() => { void profile.refetch(); }}>Try again</Button>} />;

  const snapshot = profile.data;
  const edited = editor?.fact ? (snapshot.facts ?? []).find(fact => fact.id === editor.fact?.id) : undefined;
  return <div className="profile-content">
    <p>Your accepted facts guide job evaluations. Changing them will require evaluations to refresh.</p>
    <p role="status" aria-atomic="true">{notice}</p>
    {profile.isError && <StatePanel state="stale" title="This profile may be out of date"
      description="The latest profile could not be loaded. Your draft is kept; reload before making changes."
      action={<Button loading={profile.isFetching} onClick={() => { void reload(); }}>Reload profile</Button>} />}
    {conflict && !profile.isError && <StatePanel state="stale" title="Your profile changed elsewhere"
      description="Your draft is kept. Reload and review the latest facts, then choose whether to try again."
      action={<Button loading={profile.isFetching} onClick={() => { void reload(); }}>Reload profile</Button>} />}
    {error && !conflict && !fieldError && <p role="alert">The change could not be confirmed. Your draft is kept. Reload the profile before retrying.
      {' '}<Button disabled={profile.isFetching} tone="secondary" onClick={() => { void reload(); }}>Reload profile</Button></p>}
    <section aria-labelledby="profile-skills"><h2 id="profile-skills">Skills</h2>
      <form className="profile-skill-form" noValidate onSubmit={event => {
        event.preventDefault();
        if (!name.trim() || name.length > 120) { setInvalid(true); input.current?.focus(); return; }
        setInvalid(false); run({ kind: 'add', name, revision: snapshot.revision });
      }}>
        <Field label="Skill name" ref={input} value={name} required maxLength={120} readOnly={mutation.isPending}
          {...(fieldError ? { error: fieldError } : {})} onChange={event => { setName(event.target.value); setInvalid(false); }}
          description="Add one skill at a time. Equivalent names keep the existing skill." />
        <Button type="submit" disabled={blocked} loading={mutation.isPending && mutation.variables.kind === 'add'}
          loadingLabel="Adding skill…">Add skill</Button>
      </form>
      {(snapshot.facts ?? []).filter(fact => fact.data.kind === 'skill').length === 0 && <p>No skills yet. Add a skill you want to use in your job search.</p>}
      <ul className="profile-facts">{(snapshot.facts ?? []).filter(fact => fact.data.kind === 'skill').map(fact => <li key={fact.id}>
        <FactDetails fact={fact} /><Evidence fact={fact} />
        <Button tone="secondary" disabled={blocked} aria-label={`Remove ${fact.data.kind === 'skill' ? fact.data.name : ''}`}
          loading={mutation.isPending && mutation.variables.kind === 'remove' && mutation.variables.id === fact.id}
          loadingLabel="Removing…" onClick={() => { run({ kind: 'remove', id: fact.id, revision: snapshot.revision }); }}>Remove</Button>
      </li>)}</ul>
    </section>
    {(['experience', 'project'] as const).map(kind => <section key={kind} aria-labelledby={`profile-${kind}`}>
      <h2 id={`profile-${kind}`} tabIndex={-1}>{kind === 'experience' ? 'Experience' : 'Projects'}</h2>
      <Button id={`profile-add-${kind}`} tone="secondary" disabled={blocked} onClick={event => {
        setEditor({ kind, fact: null, remove: false, revision: snapshot.revision, opener: event.currentTarget });
      }}>Add {kind}</Button>
      {!(snapshot.facts ?? []).some(fact => fact.data.kind === kind) && <p>No accepted {kind === 'experience' ? 'experience' : 'projects'} yet.</p>}
      <ul className="profile-facts">{(snapshot.facts ?? []).filter(fact => fact.data.kind === kind).map(fact => <li key={fact.id}>
        <FactDetails fact={fact} /><Evidence fact={fact} />
        <div className="profile-editor-actions">
          <Button tone="secondary" disabled={blocked} onClick={event => {
            setEditor({ kind, fact, remove: false, revision: snapshot.revision, opener: event.currentTarget });
          }}>Edit {kind}</Button>
          <Button tone="secondary" disabled={blocked} onClick={event => {
            setEditor({ kind, fact, remove: true, revision: snapshot.revision, opener: event.currentTarget });
          }}>Remove {kind}</Button>
        </div>
      </li>)}</ul>
    </section>)}
    <Documents ownerId={ownerId} />
    {editor && <FactEditor target={editor} missing={Boolean(editor.fact && !edited)}
      evidence={edited && <div><FactDetails fact={edited} /><Evidence fact={edited} /></div>}
      reload={async () => { const result = await profile.refetch(); if (!result.isSuccess) throw new Error('Profile reload failed'); return result.data; }}
      saved={async result => {
        setNotice(result.outcome === 'existing' ? 'This fact already exists. The saved values were kept.'
          : result.outcome === 'deleted' ? 'Fact removed.' : 'Fact saved.');
        await api.queryClient.invalidateQueries({ queryKey, exact: true });
      }} close={() => {
        restoreFocus.current = editor; setEditor(null);
      }} />}
  </div>;
}
