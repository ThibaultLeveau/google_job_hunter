import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useParams } from 'react-router';
import type { components } from '../../../generated/api';
import { safeReturnPath } from '../../app/routes';
import { api, ApiError, sessionKey, type Session } from '../../shared/api';
import { Badge, Button, StatePanel } from '../../shared/ui';
import { arrangements, fact, salary, sourceLink, timestamp } from './jobDisplay';
import { MatchExplanation } from './MatchExplanation';

type Detail = components['schemas']['JobDetail'];
type Citation = components['schemas']['Citation'];
const kinds = { job_requirement: 'Job requirement', material: 'Application material', instruction: 'Application instruction', deadline: 'Deadline' };
const levels = { required: 'Required', preferred: 'Preferred', unclear: 'Priority unclear' };
function Evidence({ evidence, source }: { evidence: Citation[]; source: Detail['source'] }) {
  return <div className="job-evidence">{evidence.map((span, index) => {
    const paragraph = source?.paragraphs?.findIndex(value => value.id === span.paragraph_id) ?? -1;
    return <figure key={`${span.paragraph_id}-${String(span.start)}-${String(index)}`}><blockquote>{span.quote}</blockquote>
      <figcaption>{paragraph < 0 ? 'Source paragraph unavailable' : `Source paragraph ${String(paragraph + 1)}`} · Characters {span.start}–{span.end}</figcaption></figure>;
  })}</div>;
}
function sourceUnavailable(data: Detail) {
  return data.status.freshness === 'removed' || data.metadata?.state === 'non_job' || data.source && data.source.state !== 'uncertain'
    || data.status.stages.some(stage => ['fetch', 'text'].includes(stage.stage) && stage.state === 'unsupported');
}
function Requirements({ data }: { data: Detail }) {
  const value = data.requirements;
  if (!value || value.state !== 'ready' || !value.items.length) return <p>{!value ? sourceUnavailable(data) ? 'Application requirements are unavailable for this source.' : 'Application requirements are not ready yet. Refresh to check for progress.'
    : value.state === 'failed' ? 'Application requirements could not be prepared.' : value.state === 'unavailable' ? 'Application requirements are unavailable for this source.'
      : 'Application requirements are unknown. This does not mean no requirements apply.'}</p>;
  return <><p className="job-text">{value.summary || 'Review the full checklist for application requirements.'}</p>
    {value.more_requirements > 0 && <p>{value.more_requirements} additional {value.more_requirements === 1 ? 'item is' : 'items are'} in the full checklist.</p>}
    <details className="job-disclosure"><summary>Full checklist and evidence ({value.items.length} items)</summary>
      {(Object.keys(levels) as (keyof typeof levels)[]).map(level => {
        const items = value.items.filter(item => item.level === level);
        return items.length > 0 && <section key={level} aria-labelledby={`requirements-${level}`}><h4 id={`requirements-${level}`}>{levels[level]}</h4>
          <ul className="job-requirements">{items.map(item => <li key={item.id}><Badge>{kinds[item.kind]}</Badge><p className="job-text">{item.text}</p>
            {item.kind === 'deadline' && <p>{item.deadline_at ? <>Explicit deadline: <time dateTime={item.deadline_at}>{timestamp(item.deadline_at, 'Deadline time unknown')}</time></> : 'Deadline date or time is not explicit.'}</p>}
            <details><summary>Source evidence ({item.evidence.length} excerpts)</summary><Evidence evidence={item.evidence} source={data.source} /></details></li>)}</ul>
        </section>;
      })}
    </details></>;
}

export function JobDetail({ ownerId }: { ownerId: string }) {
  const { jobId = '' } = useParams(), location = useLocation(), state: unknown = location.state;
  const supplied = typeof state === 'object' && state !== null && 'returnTo' in state && typeof state.returnTo === 'string' ? safeReturnPath(state.returnTo) : '/jobs';
  const cursors = typeof state === 'object' && state !== null && 'jobCursors' in state && Array.isArray(state.jobCursors)
    ? state.jobCursors.filter((value): value is string => typeof value === 'string').slice(-100) : [];
  const back = new URL(supplied, 'https://job-hunter.invalid').pathname === '/jobs' ? supplied : '/jobs';
  const valid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(jobId), id = jobId.toLowerCase();
  const query = useQuery({ queryKey: ['job-detail', ownerId, id], enabled: valid, staleTime: 30000, retry: false, refetchOnWindowFocus: false,
    queryFn: ({ signal }) => api.request<Detail>(`/api/v1/jobs/${encodeURIComponent(id)}`, { signal }).catch((error: unknown) => {
      if (error instanceof ApiError && [401, 403].includes(error.status)) void api.invalidateSession(); throw error;
    }) });
  useEffect(() => () => {
    if (api.queryClient.getQueryData<Session | null>(sessionKey)?.id !== ownerId) api.queryClient.removeQueries({ queryKey: ['job-detail', ownerId] });
  }, [ownerId]);
  const backLink = <Link to={back} state={{ jobCursors: cursors }}>Back to jobs</Link>;
  const unavailable = !valid || query.error instanceof ApiError && [404, 422].includes(query.error.status);
  if (unavailable) return <StatePanel state="error" title="Job unavailable" description="This job is not available in your discoveries. Return to your jobs to continue." action={backLink} />;
  if (query.isPending) return <><p>{backLink}</p><div className="jobs-loading" aria-busy="true"><p role="status">Loading job details</p><div className="job-skeleton" aria-hidden="true"><span /><span /><span /></div></div></>;
  if (!query.data) return <StatePanel state="error" title="Job details could not be loaded" description="Your discoveries are safe. Retry to load this job."
    action={<><Button loading={query.isFetching} onClick={() => { void query.refetch(); }}>Retry job details</Button>{backLink}</>} />;
  const data = query.data, metadata = data.metadata, source = sourceLink(data.source_url);
  const field = (key: string, unknown: string) => {
    const value = metadata?.fields[key]; return fact(value ? { status: value.status, values: value.values.map(item => item.value) } : {}, unknown);
  };
  const citations = [...(metadata?.selection.evidence ?? []), ...(metadata?.selection.facts.map(item => item.evidence) ?? []), ...(metadata?.selection.salaries.map(item => item.evidence) ?? [])];
  const evidence = [...new Map(citations.map(span => [`${span.paragraph_id}:${String(span.start)}:${String(span.end)}`, span])).values()];
  const unsupported = sourceUnavailable(data) && data.status.freshness !== 'removed';
  const failed = data.status.stages.some(stage => stage.state === 'failed');
  const nonJob = metadata?.state === 'non_job' || data.source?.state === 'non_job';
  return <div className="job-detail" key={data.status.id}>
    <div className="jobs-toolbar">{backLink}<Button tone="secondary" loading={query.isFetching} loadingLabel="Refreshing details…" onClick={() => { void query.refetch(); }}>Refresh details</Button></div>
    {query.isError && <p role="alert">Refresh failed. Showing the last loaded details; refresh to try again.</p>}
    <header className="job-detail-heading"><h2>{field('title', 'Job title unknown')}</h2><p>{field('employer', 'Company unknown')}</p>
      <p>{source ? <a href={source.href} target="_blank" rel="noopener noreferrer">Original posting at {source.hostname} <span aria-hidden="true">(new tab)</span><span className="visually-hidden">— opens in a new tab</span></a> : 'Original posting link unavailable'}</p>
      <p className="job-note">Fetched: {data.fetched_at ? <time dateTime={data.fetched_at}>{timestamp(data.fetched_at)}</time> : 'Not fetched yet'} · Source language: {metadata?.source_language ?? data.source?.language ?? 'Unknown'}</p>
    </header>
    {data.status.freshness === 'removed' ? <p role="status" className="job-detail-notice">The original posting was removed. Preserved content may be out of date.</p>
      : data.status.freshness === 'stale' ? <p role="status" className="job-detail-notice">Source content is stale. These details come from an earlier fetch.</p>
      : data.status.freshness === 'pending' && <p role="status" className="job-detail-notice">The source has not been fetched yet.</p>}
    {nonJob ? <p role="status">This source was classified as not a job listing.</p> : unsupported ? <p role="status">Source content is blocked or unsupported. Use the original posting when available.</p>
      : !data.status.analysis_ready && !sourceUnavailable(data) && <p role="status">{failed ? 'Some processing failed. Available facts are shown below.' : 'Some details are still processing. Refresh to check for updates.'}</p>}
    {metadata?.state === 'ambiguous' && <p role="status">Some facts are uncertain or conflicting; review their source evidence.</p>}
    <div className="job-detail-columns"><section className="job-detail-panel" aria-labelledby="job-facts-heading"><h3 id="job-facts-heading">Job facts</h3>
      <dl className="job-facts">
        {([['location', 'Location'], ['region', 'Region'], ['employment_type', 'Employment type'], ['seniority', 'Seniority'], ['skills', 'Skills'], ['geographic_eligibility', 'Geographic eligibility']] as const).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{field(key, 'Unknown')}</dd></div>)}
        <div><dt>Work arrangement</dt><dd>{arrangements[metadata?.work_arrangement ?? 'unknown']}</dd></div>
        <div><dt>Salary</dt><dd>{salary({ status: metadata?.salary_status ?? 'unknown', values: metadata?.selection.salaries ?? [] })}</dd></div>
        <div><dt>Requested languages</dt><dd>{field('language', 'Unknown')}</dd></div>
      </dl>
      {evidence.length > 0 && <details className="job-disclosure"><summary>Evidence for job facts ({evidence.length} excerpts)</summary><Evidence evidence={evidence} source={data.source} /></details>}
    </section>
    <section className="job-detail-panel" aria-labelledby="job-checklist-heading"><h3 id="job-checklist-heading">Application checklist</h3><Requirements data={data} /></section></div>
    <section className="job-detail-panel" aria-labelledby="job-content-heading"><h3 id="job-content-heading">Posting text</h3>
      {data.clean_text ? <><p>{data.clean_text.state === 'degraded' ? 'Text cleanup is incomplete. Preserved source text is shown without inferred changes.' : 'Prepared from the preserved source. Evidence excerpts refer to the original source paragraphs.'}</p>
        <details className="job-disclosure"><summary>Read prepared posting text</summary><p className="job-text">{data.clean_text.text || 'No prepared text available.'}</p></details></>
        : <p>{sourceUnavailable(data) ? 'Prepared posting text is unavailable.' : 'Prepared posting text is not available yet.'}</p>}
      {data.source?.paragraphs?.length ? <details className="job-disclosure"><summary>Read preserved source ({data.source.paragraphs.length} paragraphs)</summary>
        {data.source.paragraphs.map((paragraph, index) => <section className="job-source-paragraph" key={paragraph.id}><h4>Paragraph {index + 1}</h4><p className="job-text">{paragraph.text}</p></section>)}</details>
        : <p>Preserved source text is unavailable.</p>}
    </section>
    <MatchExplanation key={data.status.id} data={data} ownerId={ownerId} queryId={id} unavailable={sourceUnavailable(data)} refreshing={query.isFetching} />
  </div>;
}
