import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import type { components } from '../../../generated/api';
import { api, ApiError, sessionKey, type Session } from '../../shared/api';
import { Button, Field } from '../../shared/ui';
import { choices, filterErrors, filterParams, readFilters, type Filter, type Filters } from './jobFilters';

const mediaQuery = () => typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 40rem)') : undefined;
function subscribe(listener: () => void) {
  const media = mediaQuery(); media?.addEventListener('change', listener);
  return () => { media?.removeEventListener('change', listener); };
}
const mobileSnapshot = () => mediaQuery()?.matches ?? false;
function read<T>(path: `/api/${string}`, signal: AbortSignal) {
  return api.request<T>(path, { signal }).catch((error: unknown) => {
    if (error instanceof ApiError && [401, 403].includes(error.status)) void api.invalidateSession(); throw error;
  });
}
export function JobFilters({ ownerId }: { ownerId: string }) {
  const [params, setParams] = useSearchParams(), url = params.toString();
  const [state, setState] = useState(() => ({ url, draft: readFilters(params) }));
  if (state.url !== url) setState({ url, draft: readFilters(params) });
  const { draft } = state, serialized = filterParams(draft).toString(), errors = filterErrors(filterParams(draft));
  const invalid = Object.keys(errors).length > 0, applied = filterParams(readFilters(params)).toString();
  const mobile = useSyncExternalStore(subscribe, mobileSnapshot), [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null), opener = useRef<HTMLButtonElement>(null), form = useRef<HTMLFormElement>(null);
  const commit = useCallback((value: string) => { setParams(value, { state: { jobCursors: [] } }); }, [setParams]);
  useEffect(() => {
    if (invalid || serialized === applied || state.url !== url) return;
    const timer = window.setTimeout(() => { commit(serialized); }, 300);
    return () => { window.clearTimeout(timer); };
  }, [serialized, applied, invalid, commit, state.url, url]);
  useEffect(() => {
    if (!mobile || !open) return;
    const element = dialog.current, trigger = opener.current; element?.showModal(); element?.querySelector('input')?.focus();
    return () => { element?.close(); trigger?.focus(); };
  }, [mobile, open]);
  useEffect(() => () => {
    if (api.queryClient.getQueryData<Session | null>(sessionKey)?.id !== ownerId) api.queryClient.removeQueries({ queryKey: ['job-filter-runs', ownerId] });
  }, [ownerId]);
  const runs = useInfiniteQuery({ queryKey: ['job-filter-runs', ownerId, 'pages'], initialPageParam: 0,
    queryFn: ({ signal, pageParam }) => read<components['schemas']['RunPage']>(`/api/v1/searches/runs?limit=20&offset=${String(pageParam)}`, signal),
    getNextPageParam: page => page.next_offset ?? undefined, staleTime: 30000, retry: false, refetchOnWindowFocus: false });
  const available = [...new Map((runs.data?.pages.flatMap(page => page.items) ?? []).filter(run => run.status !== 'cancelled').map(run => [run.id, run])).values()];
  const selected = useQuery({ queryKey: ['job-filter-runs', ownerId, draft.run_id],
    enabled: Boolean(draft.run_id) && !errors.run_id && !runs.isPending && !available.some(run => run.id === draft.run_id),
    queryFn: ({ signal }) => read<components['schemas']['SearchRun']>(`/api/v1/searches/runs/${encodeURIComponent(draft.run_id)}`, signal),
    staleTime: 30000, retry: false, refetchOnWindowFocus: false });
  if (selected.data && selected.data.status !== 'cancelled' && !available.some(run => run.id === selected.data.id)) available.push(selected.data);
  function change(key: Filter, value: string, immediate = false) {
    const next: Filters = { ...draft, [key]: value }; setState({ url, draft: next });
    if (immediate && !Object.keys(filterErrors(filterParams(next))).length) commit(filterParams(next).toString());
  }
  function clear() { setState({ url: '', draft: readFilters(new URLSearchParams()) }); commit(''); }
  function close() { setState({ url, draft: readFilters(params) }); setOpen(false); }
  function select(key: Filter, label: string, options: ReactNode) {
    return <div className="ui-field"><label htmlFor={`job-filter-${key}`}>{label}</label>
      <select id={`job-filter-${key}`} className="ui-input" value={key === 'sort' && draft[key] === 'newest' ? '' : draft[key]} aria-invalid={Boolean(errors[key])}
        aria-describedby={errors[key] ? `job-filter-${key}-error` : undefined} onChange={event => { change(key, event.target.value, true); }}>{options}</select>
      {errors[key] && <p role="alert" id={`job-filter-${key}-error`}>{errors[key]}</p>}</div>;
  }
  function input(key: Filter, label: string, maxLength: number, numeric = false) {
    return <Field label={label} value={draft[key]} maxLength={maxLength} inputMode={numeric ? 'decimal' : 'text'}
      {...(errors[key] ? { error: errors[key] } : {})} onChange={event => { change(key, key === 'currency' ? event.target.value.toUpperCase() : event.target.value); }} />;
  }
  function unknown(key: 'region' | 'remote' | 'salary', label: string) {
    const field = `include_unknown_${key}` as const;
    return <div><label className="job-filter-check"><input type="checkbox" checked={draft[field] === 'true'} aria-invalid={Boolean(errors[field])}
      aria-describedby={errors[field] ? `${field}-error` : undefined} onChange={event => { change(field, event.target.checked ? 'true' : '', true); }} />{label}</label>
      {errors[field] && <p role="alert" id={`${field}-error`}>{errors[field]}</p>}</div>;
  }
  const content = <form ref={form} noValidate onSubmit={event => {
    event.preventDefault(); if (invalid) { form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(); return; }
    commit(serialized); setOpen(false);
  }}>
    <div className="job-filter-body"><h2 id="job-filters-title">Filter and sort jobs</h2>
    <p className="ui-field-hint">Changes apply automatically. Text waits briefly while you type.</p>
    <div className="job-filter-grid">
      {input('keyword', 'Keyword', 80)}
      {select('run_id', 'Search run', <><option value="">All search runs</option>
        {draft.run_id && !available.some(run => run.id === draft.run_id) && <option value={draft.run_id}>{selected.isFetching ? 'Loading selected run…' : 'Selected run unavailable'}</option>}
        {available.map(run => <option key={run.id} value={run.id}>{run.snapshot.name} · {new Date(run.created_at).toLocaleString()} · {run.status}</option>)}</>)}
      {input('region', 'Region (exact name)', 80)}
      {(['remote', 'match_status', 'sort'] as const).map(key => <div key={key}>{select(key, { remote: 'Work arrangement filter', match_status: 'Match status', sort: 'Sort jobs' }[key],
        <><option value="">{key === 'sort' ? choices.sort.newest : 'Any'}</option>{Object.entries(choices[key]).filter(([value]) => key !== 'sort' || value !== 'newest').map(([value, label]) => <option key={value} value={value}>{label}</option>)}</>)}</div>)}
    </div>
    {runs.isPending && <p role="status">Loading search runs…</p>}
    {runs.data && !available.length && <p>No available search runs.</p>}
    {(runs.isError || selected.isError) && <p role="alert">Search runs could not be loaded. <Button tone="secondary" onClick={() => { void runs.refetch(); if (draft.run_id && !errors.run_id) void selected.refetch(); }}>Retry search runs</Button></p>}
    {runs.hasNextPage && <Button tone="secondary" loading={runs.isFetchingNextPage} onClick={() => { void runs.fetchNextPage(); }}>Load older search runs</Button>}
    <div className="job-filter-checks">{unknown('region', 'Include unknown regions')}{unknown('remote', 'Include unknown work arrangements')}</div>
    <fieldset className="job-filter-salary"><legend>Salary</legend>
      <p className="ui-field-hint">Choose currency and period together. Amounts keep their source units; no currency or period conversions.</p>
      <div className="job-filter-grid">{input('salary_min', 'Minimum salary', 18, true)}{input('salary_max', 'Maximum salary', 18, true)}{input('currency', 'Currency', 3)}
        {select('period', 'Salary period', <><option value="">Choose period</option>{Object.entries(choices.period).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</>)}
      </div>{unknown('salary', 'Include unknown salary data')}
    </fieldset>
    <p className="ui-field-hint">Unknown toggles apply to their active filter. Known salaries with other units stay excluded.</p>
    {invalid && <p role="status">Correct the marked fields to update results.</p>}</div>
    <div className="job-filter-actions"><Button type="submit">{mobile ? 'Show results' : 'Apply filters'}</Button><Button tone="secondary" onClick={clear}>Clear all</Button>
      {mobile && <Button tone="secondary" onClick={close}>Close filters</Button>}</div>
  </form>;
  return mobile ? <><Button ref={opener} tone="secondary" aria-haspopup="dialog" onClick={() => { setOpen(true); }}>Filters and sorting</Button>
    {open && <dialog ref={dialog} className="job-filter-drawer" aria-labelledby="job-filters-title" onCancel={event => { event.preventDefault(); close(); }}
      onKeyDown={event => {
        if (event.key !== 'Tab') return;
        const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input, select')], first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first || !event.shiftKey && document.activeElement === last) { event.preventDefault(); (event.shiftKey ? last : first)?.focus(); }
      }}>{content}</dialog>}</> : <section className="job-filters" aria-labelledby="job-filters-title">{content}</section>;
}
