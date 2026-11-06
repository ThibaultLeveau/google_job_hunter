import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useSearchParams } from 'react-router';
import type { components } from '../../../generated/api';
import { api, ApiError, sessionKey, type Session } from '../../shared/api';
import { Badge, Button, StatePanel } from '../../shared/ui';

type Job = components['schemas']['JobSummary'];
type Page = components['schemas']['JobPage'];
const arrangements: Record<Job['remote'], string> = {
  remote: 'Remote', hybrid: 'Hybrid', on_site: 'On site', unknown: 'Work arrangement unknown', conflicting: 'Conflicting work arrangements',
};
function fact(value: Job['title'], unknown: string) {
  if (!value.status || value.status === 'unknown' || !value.values?.length) return unknown;
  return `${value.values.join(' · ')}${value.status === 'known' ? '' : ' (uncertain)'}`;
}
function salary(value: Job['salary']) {
  if (!value.status || value.status === 'unknown' || !value.values?.length) return 'Salary unknown';
  return value.values.map(item => {
    const amount = item.minimum === null ? item.maximum === null ? 'Amount unknown' : `Up to ${item.maximum}`
      : item.maximum === null ? `From ${item.minimum}` : item.minimum === item.maximum ? item.minimum : `${item.minimum}–${item.maximum}`;
    return `${item.currency ?? 'Currency unknown'} ${amount} / ${item.period ?? 'period unknown'} · ${item.basis ?? 'basis unknown'}${item.uncertain ? ' (uncertain)' : ''}`;
  }).join('; ') + (value.status === 'known' ? '' : ' — conflicting or uncertain salary information');
}
function sourceLink(value: string) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url : null; }
  catch { return null; }
}
function timestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Fetch time unknown' : date.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}
function matchLabel(value: Job['match']) {
  const score = value.score === null ? 'Score unknown' : `${String(value.score)}%`;
  if (value.stale) return `Stale match · ${score}`;
  if (value.status === 'failed') return 'Match unavailable';
  if (value.status === 'pending') return 'Match pending';
  return `Match · ${score}`;
}
const fetchLabels: Record<string, string> = {
  pending: 'Awaiting source', blocked: 'Source blocked', removed: 'Source removed', unsupported: 'Unsupported source', non_job: 'Not a job listing', failed: 'Fetch failed',
};
const analysisLabels: Record<Job['analysis_status'], string> = {
  pending: 'Analysis pending', ready: 'Analysis ready', unknown: 'Some details unknown', non_job: 'Not a job listing', failed: 'Analysis failed', unavailable: 'Analysis unavailable',
};

export function Jobs({ ownerId }: { ownerId: string }) {
  const location = useLocation(), [params, setParams] = useSearchParams();
  const cursor = params.get('cursor') ?? '';
  const heading = useRef<HTMLHeadingElement>(null), reset = useRef<HTMLButtonElement>(null), previous = useRef(cursor);
  const query = useQuery({ queryKey: ['jobs', ownerId, cursor], staleTime: 30000, retry: false, refetchOnWindowFocus: false,
    queryFn: ({ signal }) => api.request<Page>(`/api/v1/jobs?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { signal })
      .catch((error: unknown) => { if (error instanceof ApiError && [401, 403].includes(error.status)) void api.invalidateSession(); throw error; }) });
  useEffect(() => () => {
    if (api.queryClient.getQueryData<Session | null>(sessionKey)?.id !== ownerId) {
      api.queryClient.removeQueries({ queryKey: ['jobs', ownerId] });
      api.queryClient.removeQueries({ queryKey: ['jobs-position', ownerId] });
    }
  }, [ownerId]);
  const history: unknown = location.state;
  const prior = typeof history === 'object' && history !== null && 'jobCursors' in history && Array.isArray(history.jobCursors)
    ? history.jobCursors.filter((value): value is string => typeof value === 'string').slice(-100) : [];
  useEffect(() => {
    if (!query.isPending && previous.current !== cursor) (heading.current ?? reset.current)?.focus();
    if (!query.isPending) previous.current = cursor;
  }, [cursor, query.isPending]);
  const restored = useRef<string | null>(null);
  useEffect(() => {
    if (!query.data || restored.current === location.key) return;
    restored.current = location.key;
    const position = api.queryClient.getQueryData<number>(['jobs-position', ownerId, location.search]);
    if (position !== undefined) window.scrollTo({ top: position, behavior: 'instant' });
  }, [ownerId, location.key, location.search, query.data]);
  function page(next: string, stack: string[]) {
    const values = new URLSearchParams(params);
    if (next) values.set('cursor', next); else values.delete('cursor');
    setParams(values, { state: { jobCursors: stack } });
  }
  const refresh = <Button tone="secondary" loading={query.isFetching} loadingLabel="Refreshing jobs…" onClick={() => { void query.refetch(); }}>Refresh jobs</Button>;
  const restart = <Button ref={reset} tone="secondary" onClick={() => { page('', []); }}>Return to first page</Button>;
  if (query.isPending) return <div className="jobs-loading" aria-busy="true">
    <p role="status">Loading jobs</p>{[0, 1, 2].map(id => <div key={id} className="job-skeleton" aria-hidden="true"><span /><span /><span /></div>)}
  </div>;
  if (!query.data || query.isError && !query.data.items.length) return <StatePanel state="error" title={query.error instanceof ApiError && query.error.status === 422 ? 'This results page has expired' : 'Jobs could not be loaded'}
    description="Your saved searches are safe. Retry, or return to the first page to load current discoveries."
    action={<><Button loading={query.isFetching} onClick={() => { void query.refetch(); }}>Retry loading jobs</Button>{cursor && restart}</>} />;
  if (!query.data.items.length) return <StatePanel state="empty" title={cursor ? 'No jobs on this page' : 'No discoveries yet'}
    description={cursor ? 'Results may have changed since this page was loaded. Return to your current discoveries.' : 'Run a saved search to discover jobs. Their facts will appear here as processing finishes.'}
    action={<>{cursor ? restart : <Link to="/searches">Open saved searches</Link>}{refresh}</>} />;
  return <div className="jobs-page">
    <div className="jobs-toolbar"><div><h2 ref={heading} tabIndex={-1}>Discovered jobs</h2>
      <p>{query.data.items.length} {query.data.items.length === 1 ? 'job' : 'jobs'} on this page · Newest discoveries first</p></div>
      {refresh}</div>
    {query.isError && <p role="alert">Refresh failed. Showing the last loaded results; refresh to try again.</p>}
    <ul className="job-list" aria-label="Discovered jobs">
      {query.data.items.map(job => {
        const title = fact(job.title, 'Job title unknown'), source = sourceLink(job.source_url);
        const progress = fetchLabels[job.fetch_status] ?? analysisLabels[job.analysis_status];
        return <li key={job.id}><article className="job-card" aria-labelledby={`job-${job.id}`}>
          <div className="job-card-heading"><div><h2 id={`job-${job.id}`}><Link to={`/jobs/${encodeURIComponent(job.id)}`}
            state={{ returnTo: location.pathname + location.search }} onClick={() => {
              api.queryClient.setQueryData(['jobs-position', ownerId, location.search], window.scrollY);
            }}>{title}</Link></h2><p className="job-company">{fact(job.company, 'Company unknown')}</p></div>
            {progress !== 'Analysis ready' && <Badge tone={['failed', 'removed'].includes(job.fetch_status) || job.analysis_status === 'failed' ? 'danger' : 'neutral'}>{progress}</Badge>}</div>
          <dl className="job-facts">
            <div><dt>Location</dt><dd>{fact(job.location, fact(job.region, 'Location unknown'))}</dd></div>
            <div><dt>Work arrangement</dt><dd>{arrangements[job.remote]}</dd></div>
            <div><dt>Salary</dt><dd>{salary(job.salary)}</dd></div>
            <div><dt>Profile match</dt><dd>{matchLabel(job.match)}
              {job.match.stale && <span className="job-note">Earlier evaluation. {job.match.pending ? 'Updated match pending.' : 'Refresh to check for an update.'}</span>}
              {job.match.assessed_percent !== null && <span className="job-note">{job.match.assessed_percent}% of requirement weight assessed</span>}</dd></div>
          </dl>
          <footer className="job-source"><span>{source ? <a href={source.href} target="_blank" rel="noopener noreferrer">{source.hostname} <span aria-hidden="true">(new tab)</span><span className="visually-hidden">— original posting, opens in a new tab</span></a> : 'Source link unavailable'}</span>
            <span>Fetched: {job.fetched_at ? <time dateTime={job.fetched_at}>{timestamp(job.fetched_at)}</time> : 'Not fetched yet'}</span></footer>
        </article></li>;
      })}
    </ul>
    <nav className="jobs-pagination" aria-label="Job results pages">
      <Button tone="secondary" disabled={!cursor || query.isFetching} onClick={() => { page(prior.at(-1) ?? '', prior.slice(0, -1)); }}>Previous page</Button>
      <span aria-live="polite">{query.data.next_cursor ? 'More discoveries available' : 'End of current results'}</span>
      <Button tone="secondary" disabled={!query.data.next_cursor || query.isFetching} onClick={() => { if (query.data.next_cursor) page(query.data.next_cursor, [...prior, cursor]); }}>Next page</Button>
    </nav>
  </div>;
}
