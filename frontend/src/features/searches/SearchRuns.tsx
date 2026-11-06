import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError } from '../../shared/api';
import { Badge, Button } from '../../shared/ui';
import { isProcessing, processingHelp } from '../../shared/processing';
import type { SavedSearch } from './SearchEditor';

type Run = components['schemas']['SearchRun'];
type Attempt = { search: SavedSearch; key: string };
const active = (run: Run) => isProcessing(run.status);
const labels: Record<Run['status'], string> = { pending: 'Discovery queued', running: 'Discovering', completed: 'Discovery complete', partial: 'Partial discovery', failed: 'Discovery failed', cancelled: 'Discovery cancelled' };
function failureHelp(code: string) {
  if (['disabled', 'unconfigured', 'authentication', 'quota', 'rate_limited'].includes(code)) return processingHelp(code, 'Search');
  if (code === 'input_limit' || code === 'invalid_response') return 'Discovery could not use these results. Review the search before starting another run.';
  return 'Discovery stopped after a temporary failure. Review the results before starting another run.';
}

export function SearchRuns({ ownerId, searches, disabled, reloadSearches }: {
  ownerId: string; searches: SavedSearch[]; disabled: boolean; reloadSearches: () => Promise<void>;
}) {
  const prefix = ['search-runs', ownerId] as const;
  const [offset, setOffset] = useState(0), [selected, setSelected] = useState(''), [notice, setNotice] = useState('');
  const [attempt, setAttempt] = useState<Attempt | null>(null), [reloading, setReloading] = useState(false);
  const [retryAt, setRetryAt] = useState(0), [now, setNow] = useState(Date.now);
  const request = useRef<AbortController | null>(null), busy = useRef(false), mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false; request.current?.abort();
      api.queryClient.removeQueries({ queryKey: ['search-runs', ownerId] });
      api.queryClient.removeQueries({ queryKey: ['search-provider', ownerId] });
    };
  }, [ownerId]);
  const wait = Math.max(0, Math.ceil((retryAt - now) / 1000));
  useEffect(() => {
    if (!wait) return;
    const timer = window.setTimeout(() => { setNow(Date.now()); }, 1000);
    return () => { window.clearTimeout(timer); };
  }, [wait, now]);
  function failed(error: unknown) {
    if (error instanceof ApiError && [401, 403].includes(error.status)) void api.invalidateSession();
    if (mounted.current && error instanceof ApiError && error.status === 429) {
      const delay = Number.isFinite(error.retryAfterMs) && error.retryAfterMs > 0 ? error.retryAfterMs : 60000;
      setNow(Date.now()); setRetryAt(Date.now() + delay);
    }
  }
  const provider = useQuery({ queryKey: ['search-provider', ownerId], retry: false, refetchOnWindowFocus: false,
    queryFn: ({ signal }) => api.request<components['schemas']['SearchCapability']>('/api/v1/searches/provider', { signal }).catch((error: unknown) => { failed(error); throw error; }) });
  const history = useQuery({ queryKey: [...prefix, offset], retry: false, refetchOnWindowFocus: false,
    queryFn: ({ signal }) => api.request<components['schemas']['RunPage']>(`/api/v1/searches/runs?limit=20&offset=${String(offset)}`, { signal }).catch((error: unknown) => { failed(error); throw error; }),
    refetchInterval: query => !query.state.error && query.state.data?.items.some(active) ? 5000 : false, refetchIntervalInBackground: false });
  const start = useMutation({ mutationFn: (value: Attempt) => {
    const controller = new AbortController(); request.current = controller;
    return api.request<Run>(`/api/v1/searches/${encodeURIComponent(value.search.id)}/runs`, { method: 'POST', signal: controller.signal,
      body: { expected_revision: value.search.revision, idempotency_key: value.key } });
  }, onError: failed, onSettled: () => { busy.current = false; }, onSuccess: async () => {
    await api.queryClient.invalidateQueries({ queryKey: prefix });
    if (mounted.current) { setAttempt(null); setOffset(0); setNotice('Run request confirmed. Check discovery history below.'); }
  } });
  const cancel = useMutation({ mutationFn: (id: string) => {
    const controller = new AbortController(); request.current = controller;
    return api.request<Run>(`/api/v1/searches/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST', signal: controller.signal });
  }, onError: failed, onSettled: () => { busy.current = false; }, onSuccess: async value => {
    await api.queryClient.invalidateQueries({ queryKey: prefix });
    if (mounted.current) setNotice(`${labels[value.status]}. Existing discoveries and shared job processing are kept.`);
  } });
  const working = start.isPending || cancel.isPending || reloading;
  const available = provider.data && ['configured', 'fixture'].includes(provider.data.status) && !provider.isError;
  const conflict = start.error instanceof ApiError && [404, 409].includes(start.error.status);
  const chosen = searches.find(value => value.id === selected);
  async function refreshSearches() {
    if (working || wait) return;
    setReloading(true);
    try { await reloadSearches(); if (mounted.current) { start.reset(); setAttempt(null); setNotice('Searches reloaded. Review the saved version before running.'); } }
    catch { if (mounted.current) setNotice('Saved searches could not be reloaded. Try again.'); }
    finally { if (mounted.current) setReloading(false); }
  }
  return <section aria-labelledby="search-runs-title">
    <h2 id="search-runs-title">Run a saved search</h2>
    <p>Start discovery manually using a saved version. Discovery completion does not mean job analysis is complete.</p>
    {provider.isPending && <p role="status">Checking search availability…</p>}
    {provider.isError && <p role="alert">Search availability could not be checked. <Button disabled={Boolean(wait)} loading={provider.isFetching} onClick={() => { void provider.refetch(); }}>Check search availability</Button></p>}
    {provider.data && !available && !provider.isError && <p role="status">Search configuration is required. Contact your workspace administrator.</p>}
    {available && <p>Each run checks at most {provider.data.max_pages} pages and {provider.data.max_results} results.</p>}
    <div className="ui-field"><label htmlFor="search-run-selection">Saved search on this page</label>
      <select id="search-run-selection" className="ui-input" value={selected} disabled={disabled || working || Boolean(attempt)} onChange={event => { setSelected(event.target.value); start.reset(); setNotice(''); }}>
        <option value="">Choose a saved search</option>{searches.map(value => <option key={value.id} value={value.id}>{value.name} · revision {value.revision}</option>)}
      </select>
    </div>
    {attempt && <p>Requested search: {attempt.search.name} · revision {attempt.search.revision}</p>}
    {attempt && start.isError && <p role="alert">{conflict ? 'This saved search changed or was deleted. Reload and review before running.'
      : start.error instanceof ApiError && start.error.status === 429 ? 'The request was rate limited. Wait before retrying.'
        : 'The run request could not be confirmed. Retry the same request to recover its outcome without creating a duplicate.'}</p>}
    {conflict && <Button loading={reloading} disabled={Boolean(wait)} onClick={() => { void refreshSearches(); }}>Reload searches before running</Button>}
    <div className="search-actions"><Button disabled={disabled || !available || working || Boolean(wait) || conflict || !chosen && !attempt}
      loading={start.isPending} loadingLabel="Starting discovery…" onClick={() => {
        if (busy.current || Date.now() < retryAt || !chosen && !attempt) return;
        const value = attempt ?? { search: chosen as SavedSearch, key: crypto.randomUUID() };
        busy.current = true; setAttempt(value); setNotice(''); start.mutate(value);
      }}>{attempt ? 'Retry same run request' : 'Run search'}</Button></div>
    {wait > 0 && <p role="status">Too many requests. Retry is available in {wait} seconds.</p>}
    <p role="status">{notice}</p>
    <h3>Discovery history</h3>
    <p>Newest first. Active runs refresh every five seconds while this page is visible.</p>
    <Button tone="secondary" loading={history.isFetching} disabled={working || Boolean(wait)} onClick={() => { void history.refetch(); }}>Refresh run history</Button>
    {!history.data && !history.isError && <p role="status">Loading discovery history…</p>}
    {history.isError && <p role="alert">Run history could not be refreshed. Displayed statuses may be out of date; refresh before relying on them.</p>}
    {cancel.isError && <p role="alert">Cancellation could not be confirmed. Refresh history or try cancelling again.</p>}
    {history.data && <>
      {history.data.items.length === 0 && <p>{offset ? 'No runs on this page.' : 'No discovery runs yet.'}</p>}
      <ul className="search-list">{history.data.items.map(run => <li key={run.id}>
        <h4>{run.snapshot.name} · revision {run.search_revision}</h4>
        <p><time dateTime={run.created_at}>{new Date(run.created_at).toLocaleString()}</time></p>
        <Badge tone={run.status === 'failed' || run.status === 'partial' ? 'danger' : 'neutral'}>{labels[run.status]}</Badge>
        {run.status === 'partial' && <p>Some discovery pages failed. Counts cover the pages completed so far.</p>}
        {run.discovery?.limited && <p>The configured page or result limit was reached; additional results may exist.</p>}
        {run.discovery?.error_code && <p>{failureHelp(run.discovery.error_code)}</p>}
        <p>Found: {run.counters.found_results ?? 0} · New URLs: {run.counters.new_urls ?? 0} · Existing URLs: {run.counters.known_urls ?? 0} · Skipped: {run.counters.skipped_results ?? 0}</p>
        <p>Discovery pages completed: {run.discovery?.pages_completed ?? 0}{run.discovery?.max_pages == null ? ' (limit pending)' : ` / ${String(run.discovery.max_pages)}`}</p>
        <p>Job processing: {run.counters.processing_ready ?? 0} ready / {run.counters.processing_total ?? 0} associated · {run.counters.processing_failed ?? 0} failed. Job analysis continues separately.</p>
        {active(run) && <Button tone="secondary" disabled={working || Boolean(wait)} loading={cancel.isPending && cancel.variables === run.id} loadingLabel="Cancelling discovery…"
          aria-label={`Cancel discovery for ${run.snapshot.name}`} onClick={() => {
            if (busy.current || Date.now() < retryAt) return; busy.current = true; setNotice(''); cancel.mutate(run.id);
          }}>Cancel discovery</Button>}
      </li>)}</ul>
      <nav className="search-actions" aria-label="Run history pages">
        <Button tone="secondary" disabled={working || history.isFetching || Boolean(wait) || offset === 0} onClick={() => { setOffset(Math.max(0, offset - 20)); }}>Newer runs</Button>
        <Button tone="secondary" disabled={working || history.isFetching || Boolean(wait) || history.data.next_offset == null} onClick={() => { setOffset(history.data.next_offset ?? offset); }}>Older runs</Button>
      </nav>
    </>}
  </section>;
}
