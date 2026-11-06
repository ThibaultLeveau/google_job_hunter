import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useMutation } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError } from '../../shared/api';
import { Button, Field } from '../../shared/ui';

type Fact = components['schemas']['ProfileFact'];
type Result = components['schemas']['FactResult'];
type Snapshot = components['schemas']['ProfileSnapshot'];
export type EditorTarget = {
  kind: 'experience' | 'project'; fact: Fact | null; remove: boolean; revision: number; opener: HTMLElement;
};
type Draft = { name: string; company: string; title: string; url: string; start: string; end: string; ongoing: string; description: string };

function errors(draft: Draft, kind: EditorTarget['kind']) {
  const result: Partial<Record<keyof Draft, string>> = {};
  for (const field of kind === 'experience' ? ['company', 'title'] as const : ['name'] as const) {
    if (!draft[field].trim() || draft[field].length > 200) result[field] = 'Enter 1–200 characters.';
  }
  for (const field of ['start', 'end'] as const) {
    const value = draft[field], date = new Date(value);
    if (value && (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000') || Number.isNaN(date.getTime())
      || date.toISOString().slice(0, 10) !== value)) result[field] = 'Use a valid date from year 0001 to 9999.';
  }
  if (draft.start && draft.end && draft.start > draft.end) result.end = 'End date must be on or after start date.';
  if (draft.ongoing === 'yes' && draft.end) result.end = 'Clear the end date for an ongoing activity.';
  if (draft.description.length > 4000) result.description = 'Use at most 4,000 characters.';
  if (kind === 'project' && draft.url.trim()) {
    try {
      const url = new URL(draft.url.trim());
      if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password
        || /\s/.test(draft.url.trim()) || draft.url.length > 2048) throw new Error('Invalid URL');
    } catch { result.url = 'Use an HTTP or HTTPS link without credentials.'; }
  }
  return result;
}

export function FactEditor({ target, missing, evidence, reload, saved, close }: {
  target: EditorTarget; missing: boolean; evidence: ReactNode;
  reload: () => Promise<Snapshot>; saved: (result: Result) => Promise<void>; close: () => void;
}) {
  const data = target.fact?.data;
  const dated = data?.kind === 'experience' || data?.kind === 'project' ? data : undefined;
  const [draft, setDraft] = useState<Draft>(() => ({
    name: data?.kind === 'project' ? data.name : '', company: data?.kind === 'experience' ? data.company : '',
    title: data?.kind === 'experience' ? data.title : '', url: data?.kind === 'project' ? data.url ?? '' : '',
    start: dated?.start_date ?? '', end: dated?.end_date ?? '', description: dated?.description ?? '',
    ongoing: dated?.ongoing == null ? 'unknown' : dated.ongoing ? 'yes' : 'no',
  }));
  const [invalid, setInvalid] = useState<ReturnType<typeof errors>>({});
  const [revision, setRevision] = useState(target.revision);
  const [reloading, setReloading] = useState(false);
  const [notice, setNotice] = useState('');
  const dialog = useRef<HTMLDialogElement>(null);
  const first = useRef<HTMLInputElement>(null), cancel = useRef<HTMLButtonElement>(null);
  const busy = useRef(false);
  const mounted = useRef(false);
  const mutation = useMutation({ mutationFn: () => {
    if (target.remove && target.fact) return api.request<Result>(
      `/api/v1/profile/facts/${encodeURIComponent(target.fact.id)}?expected_revision=${String(revision)}`, { method: 'DELETE' });
    const optional = { start_date: draft.start || null, end_date: draft.end || null,
      ongoing: draft.ongoing === 'unknown' ? null : draft.ongoing === 'yes', description: draft.description.trim() ? draft.description : null };
    const body: components['schemas']['FactWrite'] = { expected_revision: revision, fact: target.kind === 'experience'
      ? { kind: 'experience', company: draft.company, title: draft.title, ...optional }
      : { kind: 'project', name: draft.name, url: draft.url.trim() ? draft.url : null, ...optional } };
    return api.request<Result>(target.fact ? `/api/v1/profile/facts/${encodeURIComponent(target.fact.id)}` : '/api/v1/profile/facts',
      { method: target.fact ? 'PUT' : 'POST', body });
  }, onSuccess: async result => { await saved(result); if (mounted.current) dismiss(); }, onError: error => {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) void api.invalidateSession();
  }, onSettled: () => { busy.current = false; } });
  const conflict = mutation.error instanceof ApiError && (mutation.error.status === 409 || mutation.error.status === 404);
  const pending = mutation.isPending || reloading;
  useEffect(() => {
    const element = dialog.current;
    mounted.current = true;
    element?.showModal(); (target.remove ? cancel.current : first.current)?.focus();
    return () => { mounted.current = false; element?.close(); };
  }, [target.remove]);
  function dismiss() { dialog.current?.close(); close(); }
  function change(field: keyof Draft, value: string) { setDraft(previous => ({ ...previous, [field]: value })); }
  async function refresh() {
    if (reloading) return;
    setReloading(true);
    try { const latest = await reload(); setRevision(latest.revision); mutation.reset(); setNotice('Latest facts loaded. Your unsaved values are kept; review them before saving.'); }
    catch { setNotice('The latest profile could not be loaded. Your unsaved values are kept. Try reloading again.'); }
    finally { setReloading(false); }
  }
  const label = target.kind === 'experience' ? 'experience' : 'project';
  return <dialog ref={dialog} tabIndex={-1} className="profile-dialog" aria-labelledby="fact-editor-title" aria-describedby="fact-editor-help"
    onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary')]
        .filter(element => element.tagName === 'SUMMARY' || !element.closest('details:not([open])'));
      const firstControl = controls[0], lastControl = controls.at(-1);
      if (!firstControl || (event.shiftKey ? document.activeElement === firstControl : document.activeElement === lastControl)) {
        event.preventDefault(); (event.shiftKey ? lastControl : firstControl)?.focus();
        if (!firstControl) event.currentTarget.focus();
      }
    }}
    onCancel={event => { event.preventDefault(); if (!pending && !busy.current) dismiss(); }}>
    <h2 id="fact-editor-title">{target.remove ? 'Remove' : target.fact ? 'Edit' : 'Add'} {label}</h2>
    <p id="fact-editor-help">{target.remove
      ? 'Remove this accepted fact and its evidence links? The source document is kept.'
      : 'Your changes are manual. Blank optional fields stay unknown. Only evidence that still supports your values is kept.'}</p>
    {evidence}
    {missing && <p role="alert">This fact was removed elsewhere. Your draft is kept for review; close this editor to add a new fact.</p>}
    {mutation.error && <div role="alert">
      <p>{conflict ? 'The profile changed or this content conflicts with an existing fact. Reload and review before retrying.'
        : mutation.error instanceof ApiError && mutation.error.status === 422 ? 'Some values were rejected. Check the fields and try again.'
        : 'The change could not be confirmed. Your values are kept; reload before trying again.'}</p>
      <Button tone="secondary" loading={reloading} onClick={() => { void refresh(); }}>Reload latest facts</Button>
    </div>}
    <p role="status">{notice}</p>
    <form noValidate onSubmit={event => {
      event.preventDefault(); if (busy.current || pending || conflict || missing) return;
      const found = target.remove ? {} : errors(draft, target.kind); setInvalid(found);
      const firstError = Object.keys(found)[0];
      if (firstError) { dialog.current?.querySelector<HTMLElement>(`[name="${firstError}"]`)?.focus(); return; }
      busy.current = true; setNotice(''); mutation.mutate();
    }}>
      {!target.remove && <fieldset disabled={pending} className="profile-editor-fields">
        <legend className="visually-hidden">{label} details</legend>
        {target.kind === 'experience' ? <>
          <Field ref={first} label="Company" name="company" value={draft.company} required maxLength={200}
            {...(invalid.company ? { error: invalid.company } : {})} onChange={event => { change('company', event.target.value); }} />
          <Field label="Job title" name="title" value={draft.title} required maxLength={200}
            {...(invalid.title ? { error: invalid.title } : {})} onChange={event => { change('title', event.target.value); }} />
        </> : <>
          <Field ref={first} label="Project name" name="name" value={draft.name} required maxLength={200}
            {...(invalid.name ? { error: invalid.name } : {})} onChange={event => { change('name', event.target.value); }} />
          <Field label="Project link (optional)" name="url" type="url" value={draft.url} maxLength={2048}
            {...(invalid.url ? { error: invalid.url } : {})} onChange={event => { change('url', event.target.value); }} />
        </>}
        <div className="profile-date-fields">{(['start', 'end'] as const).map(field => <Field key={field} name={field}
          label={`${field === 'start' ? 'Start' : 'End'} date (optional)`} type="date" value={draft[field]} min="0001-01-01" max="9999-12-31"
          {...(invalid[field] ? { error: invalid[field] } : {})} onChange={event => { change(field, event.target.value); }} />)}</div>
        <label className="ui-field">Ongoing?
          <select className="ui-input" value={draft.ongoing} onChange={event => { change('ongoing', event.target.value); }}>
            <option value="unknown">Unknown</option><option value="yes">Yes</option><option value="no">No</option>
          </select>
        </label>
        <div className="ui-field"><label htmlFor="fact-description">Description (optional)</label>
          <textarea id="fact-description" name="description" className="ui-input" rows={4} maxLength={4000} value={draft.description}
            aria-invalid={Boolean(invalid.description)} aria-describedby={invalid.description ? 'description-error' : undefined}
            onChange={event => { change('description', event.target.value); }} />
          {invalid.description && <p role="alert" id="description-error">{invalid.description}</p>}
        </div>
      </fieldset>}
      <div className="profile-editor-actions">
        <Button ref={cancel} tone="secondary" disabled={pending} onClick={dismiss}>Cancel</Button>
        <Button type="submit" tone={target.remove ? 'danger' : 'primary'} disabled={pending || conflict || missing}
          loading={mutation.isPending} loadingLabel={target.remove ? 'Removing…' : 'Saving…'}>{target.remove ? 'Confirm removal' : 'Save changes'}</Button>
      </div>
    </form>
  </dialog>;
}
