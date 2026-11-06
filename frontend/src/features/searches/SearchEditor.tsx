import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError } from '../../shared/api';
import { Button, Field } from '../../shared/ui';

export type SavedSearch = components['schemas']['SavedSearch'];
type Definition = components['schemas']['SearchDefinition'];
type Term = { id: string; value: string };
const term = (value = ''): Term => ({ id: crypto.randomUUID(), value });
const group = (values = ['']) => ({ id: crypto.randomUUID(), terms: values.map(term) });
const normalized = (value: string) => value.normalize('NFKC').trim().replace(/\s+/gu, ' ');

export function SearchLogic({ definition }: { definition: Definition }) {
  return <div className="search-logic" aria-label="Search logic">
    {definition.groups.map((terms, index) => <div key={index}>
      {index > 0 && <strong className="search-and">AND</strong>}
      <span>Match any of: </span>{terms.map((value, number) => <span key={number}>
        {number > 0 && <strong> OR </strong>}<span className="search-term">{value.trim() || 'Empty term'}</span>
      </span>)}
    </div>)}
    {(definition.exclusions ?? []).length > 0 && <p>Exclude: {(definition.exclusions ?? []).join(' · ')}</p>}
  </div>;
}

export function SearchEditor({ initial, refreshList, saved, close }: {
  initial: SavedSearch | null; refreshList: () => Promise<void>;
  saved: (value: SavedSearch) => Promise<void>; close: () => void;
}) {
  const [basis, setBasis] = useState(initial);
  const [name, setName] = useState(initial?.name ?? '');
  const [groups, setGroups] = useState(() => (initial?.definition.groups ?? [['']]).map(group));
  const [exclusions, setExclusions] = useState(() => (initial?.definition.exclusions ?? []).map(term));
  const [invalid, setInvalid] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState('');
  const [latest, setLatest] = useState<SavedSearch | null>(null);
  const [missing, setMissing] = useState(false), [reloading, setReloading] = useState(false);
  const first = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null), focus = useRef<string | null>(null);
  const busy = useRef(false), mounted = useRef(false);
  useEffect(() => {
    mounted.current = true; first.current?.focus();
    return () => { mounted.current = false; request.current?.abort(); };
  }, []);
  useLayoutEffect(() => {
    if (focus.current) { document.getElementById(focus.current)?.focus(); focus.current = null; }
  }, [groups, exclusions]);
  const definition: Definition = { ...basis?.definition, groups: groups.map(value => value.terms.map(item => item.value)), exclusions: exclusions.map(item => item.value) };
  const mutation = useMutation({ mutationFn: () => {
    const controller = new AbortController(); request.current = controller;
    const body: components['schemas']['SearchWrite'] | components['schemas']['SearchEdit'] = {
      name, definition, ...(basis ? { expected_revision: basis.revision } : {}),
    };
    return api.request<SavedSearch>(basis ? `/api/v1/searches/${encodeURIComponent(basis.id)}` : '/api/v1/searches',
      { method: basis ? 'PUT' : 'POST', body, signal: controller.signal });
  }, onSuccess: async result => { if (mounted.current) await saved(result); }, onError: error => {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) void api.invalidateSession();
  }, onSettled: () => { busy.current = false; } });
  const pending = mutation.isPending || reloading;
  const requiresReload = Boolean(mutation.error && !(mutation.error instanceof ApiError && mutation.error.status === 422));
  const count = groups.reduce((total, value) => total + value.terms.length, 0);
  async function reload() {
    if (busy.current || pending) return;
    busy.current = true; setReloading(true);
    const controller = new AbortController(); request.current = controller;
    try {
      await refreshList();
      if (basis) {
        const value = await api.request<SavedSearch>(`/api/v1/searches/${encodeURIComponent(basis.id)}`, { signal: controller.signal });
        if (mounted.current) { setBasis(value); setLatest(value); }
      }
      if (mounted.current) { mutation.reset(); setNotice('Latest searches loaded. Your draft is kept; review saved values before saving again.'); }
    } catch (error) {
      if (mounted.current) {
        if (error instanceof ApiError && error.status === 404) setMissing(true);
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) void api.invalidateSession();
        setNotice('The latest search could not be loaded. Your draft is kept.');
      }
    } finally { busy.current = false; if (mounted.current) { setReloading(false); first.current?.focus(); } }
  }
  function change(id: string) { setInvalid(previous => Object.fromEntries(Object.entries(previous).filter(([key]) => key !== id))); }
  return <section className="search-editor" aria-labelledby="search-editor-title">
    <h2 id="search-editor-title">{initial ? 'Edit search' : 'New search'}</h2>
    <p>Match every group (AND), with any term inside each group (OR). A term may be a phrase; enter it without quotation marks.</p>
    <p id="search-limits">Up to 6 groups, 8 terms per group and 32 terms total. Terms and exclusions allow 1–80 characters.</p>
    {mutation.error && <div role="alert"><p>{mutation.error instanceof ApiError && mutation.error.status === 422
      ? 'Some values were rejected. Check the name and limits; use literal words or phrases without quotes, operators or URLs.'
      : 'The save could not be confirmed, or this search changed elsewhere. Your draft is kept. Reload and review before saving again.'}</p>
      {requiresReload && <Button tone="secondary" loading={reloading} onClick={() => { void reload(); }}>Reload saved searches</Button>}
    </div>}
    {missing && <p role="alert">This search was deleted elsewhere. Your draft is kept for review; cancel to return to the list.</p>}
    <p role="status">{notice}</p>
    {latest && <details open><summary>Latest saved version: {latest.name}</summary><SearchLogic definition={latest.definition} /></details>}
    <form noValidate onSubmit={event => {
      event.preventDefault(); if (busy.current || pending || requiresReload || missing) return;
      const errors: Record<string, string> = {};
      if (!name.trim() || Array.from(name).length > 120) errors['search-name'] = 'Enter a search name of 1–120 characters.';
      for (const item of [...groups.flatMap(value => value.terms), ...exclusions]) {
        if (!item.value.trim() || Array.from(item.value).length > 80 || Array.from(normalized(item.value)).length > 80) errors[item.id] = 'Enter words or a phrase of 1–80 characters.';
      }
      setInvalid(errors); const firstError = Object.keys(errors)[0];
      if (firstError) { document.getElementById(firstError)?.focus(); return; }
      busy.current = true; setNotice(''); mutation.mutate();
    }}>
      <fieldset disabled={pending} className="search-fields"><legend className="visually-hidden">Search definition</legend>
        <Field id="search-name" ref={first} label="Search name" value={name} required maxLength={120}
          {...(invalid['search-name'] ? { error: invalid['search-name'] } : {})} onChange={event => { setName(event.target.value); change('search-name'); }} />
        {groups.map((value, index) => <fieldset key={value.id} className="search-group" aria-describedby="search-limits">
          <legend>Group {index + 1}: match any term (OR)</legend>
          {index > 0 && <p className="search-and">AND the previous groups</p>}
          {value.terms.map((item, number) => <div className="search-term-row" key={item.id}>
            <Field id={item.id} label={`Group ${String(index + 1)} term ${String(number + 1)}`} required value={item.value} maxLength={80}
              {...(invalid[item.id] ? { error: invalid[item.id] } : {})} onChange={event => {
                setGroups(previous => previous.map(g => g.id === value.id ? { ...g, terms: g.terms.map(t => t.id === item.id ? { ...t, value: event.target.value } : t) } : g)); change(item.id);
              }} />
            <Button tone="secondary" disabled={value.terms.length === 1} aria-label={`Remove term ${String(number + 1)} from group ${String(index + 1)}`} onClick={() => {
              const terms = value.terms.filter(t => t.id !== item.id); focus.current = terms[Math.min(number, terms.length - 1)]?.id ?? null;
              setGroups(previous => previous.map(g => g.id === value.id ? { ...g, terms } : g));
            }}>Remove term</Button>
          </div>)}
          {new Set(value.terms.map(item => normalized(item.value))).size < value.terms.length && <p>Repeated terms in this group count once when the server prepares the query. They still count toward the editing limits.</p>}
          <div className="search-actions"><Button tone="secondary" disabled={value.terms.length >= 8 || count >= 32} onClick={() => {
            const item = term(); focus.current = item.id; setGroups(previous => previous.map(g => g.id === value.id ? { ...g, terms: [...g.terms, item] } : g));
          }}>Add OR term to group {index + 1}</Button>
          <Button tone="secondary" disabled={groups.length === 1} onClick={() => {
            const remaining = groups.filter(g => g.id !== value.id); focus.current = remaining[Math.min(index, remaining.length - 1)]?.terms[0]?.id ?? null; setGroups(remaining);
          }}>Remove group {index + 1}</Button></div>
        </fieldset>)}
        <Button tone="secondary" disabled={groups.length >= 6 || count >= 32} onClick={() => {
          const value = group(); focus.current = value.terms[0]?.id ?? null; setGroups(previous => [...previous, value]);
        }}>Add AND group</Button>
        <fieldset className="search-group"><legend>Exclude words or phrases (optional)</legend>
          {exclusions.map((item, index) => <div className="search-term-row" key={item.id}>
            <Field id={item.id} label={`Exclusion ${String(index + 1)}`} value={item.value} required maxLength={80}
              {...(invalid[item.id] ? { error: invalid[item.id] } : {})} onChange={event => { setExclusions(previous => previous.map(t => t.id === item.id ? { ...t, value: event.target.value } : t)); change(item.id); }} />
            <Button tone="secondary" aria-label={`Remove exclusion ${String(index + 1)}`} onClick={() => {
              const remaining = exclusions.filter(t => t.id !== item.id); focus.current = remaining[Math.min(index, remaining.length - 1)]?.id ?? 'search-add-exclusion'; setExclusions(remaining);
            }}>Remove exclusion</Button>
          </div>)}
          <Button id="search-add-exclusion" tone="secondary" disabled={exclusions.length >= 8} onClick={() => { const item = term(); focus.current = item.id; setExclusions(previous => [...previous, item]); }}>Add exclusion</Button>
        </fieldset>
      </fieldset>
      <h3>Your search logic</h3><SearchLogic definition={definition} />
      <div className="search-actions"><Button tone="secondary" disabled={pending} onClick={close}>Cancel editing</Button>
        <Button type="submit" disabled={pending || requiresReload || missing} loading={mutation.isPending} loadingLabel="Saving search…">Save search</Button></div>
    </form>
  </section>;
}
