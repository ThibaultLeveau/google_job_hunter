import { useEffect, useRef } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError, sessionKey, type Session } from '../../shared/api';
import { isProcessing, processingHelp, processingStates } from '../../shared/processing';
import { Badge, Button } from '../../shared/ui';
import { timestamp } from './jobDisplay';

type Status = components['schemas']['JobStatus'];
type Stage = components['schemas']['StageStatus'];
type Retry = components['schemas']['RetryWrite'];
const stages: Record<Stage['stage'], string> = { fetch: 'Posting download', text: 'Readable text', clean: 'Prepared posting', chunks: 'Source evidence sections', embed: 'Searchable evidence', metadata: 'Job facts', requirements: 'Application requirements' };
const active = (status: Status) => status.stages.some(stage => stage.generation !== null && isProcessing(stage.state))
  || status.freshness === 'current' && status.analysis_ready && status.match_pending && status.match_state !== 'failed';

export function JobProcessing({ initial, updatedAt, ownerId, queryId }: { initial: Status; updatedAt: number; ownerId: string; queryId: string }) {
  const key = ['job-status', ownerId, queryId], detailKey = ['job-detail', ownerId, queryId];
  const attempts = useRef(new Map<string, Retry>()), busy = useRef(false);
  const activeOwner = () => api.queryClient.getQueryData<Session | null>(sessionKey)?.id === ownerId;
  function recover(error: unknown) {
    if (!activeOwner()) return;
    if (error instanceof ApiError && [401, 403].includes(error.status)) void api.invalidateSession();
    else if (error instanceof ApiError && error.status === 404) void api.queryClient.invalidateQueries({ queryKey: detailKey, exact: true });
  }
  const query = useQuery({ queryKey: key, initialData: initial, initialDataUpdatedAt: updatedAt, staleTime: Infinity, retry: false,
    refetchOnMount: false, refetchOnWindowFocus: false, refetchIntervalInBackground: false,
    queryFn: ({ signal }) => api.request<Status>(`/api/v1/jobs/${encodeURIComponent(queryId)}/status`, { signal }).catch((error: unknown) => { recover(error); throw error; }),
    refetchInterval: query => query.state.error && query.state.errorUpdatedAt >= updatedAt ? false
      : active(query.state.data && query.state.dataUpdatedAt > updatedAt ? query.state.data : initial) ? 5000 : false,
  });
  const status = query.dataUpdatedAt > updatedAt ? query.data : initial;
  const stale = query.isError && query.errorUpdatedAt >= updatedAt;
  useEffect(() => () => { api.queryClient.removeQueries({ queryKey: ['job-status', ownerId, queryId], exact: true }); }, [ownerId, queryId]);
  useEffect(() => {
    if (query.dataUpdatedAt < updatedAt || JSON.stringify(query.data) === JSON.stringify(initial)) return;
    void api.queryClient.invalidateQueries({ queryKey: ['job-detail', ownerId, queryId], exact: true });
    void api.queryClient.invalidateQueries({ queryKey: ['jobs', ownerId] });
    void api.queryClient.invalidateQueries({ queryKey: ['search-runs', ownerId] });
  }, [initial, updatedAt, query.data, query.dataUpdatedAt, ownerId, queryId]);
  const mutation = useMutation({
    mutationFn: (body: Retry) => api.request<components['schemas']['WorkAccepted']>(`/api/v1/jobs/${encodeURIComponent(status.id)}/retry`, { method: 'POST', body }),
    onSuccess: async () => {
      if (!activeOwner()) return;
      await api.queryClient.invalidateQueries({ queryKey: key, exact: true });
    },
    onError: (error: unknown) => { recover(error); if (activeOwner() && error instanceof ApiError && [409, 429].includes(error.status)) void api.queryClient.invalidateQueries({ queryKey: key, exact: true }); },
    onSettled: () => { busy.current = false; },
  });
  function retry(stage: Stage) {
    if (busy.current || stale || query.isFetching || !stage.retryable || stage.generation === null || status.retries_remaining === 0) return;
    const identity = `${stage.stage}:${stage.revision}:${String(stage.generation)}`;
    let body = attempts.current.get(identity);
    if (!body) { body = { stage: stage.stage, revision: stage.revision, expected_generation: stage.generation, idempotency_key: crypto.randomUUID() }; attempts.current.set(identity, body); }
    busy.current = true; mutation.mutate(body);
  }
  const error = mutation.error;
  return <section className="job-detail-panel" aria-labelledby="job-processing-heading"><h3 id="job-processing-heading">Processing status</h3>
    <p>{active(status) ? 'Active work refreshes every five seconds while this page is visible.' : 'No active work is reported. Refresh to check for changes.'} Available job content remains usable.</p>
    <Button tone="secondary" loading={query.isFetching} loadingLabel="Refreshing processing status…" onClick={() => { void query.refetch(); }}>Refresh processing status</Button>
    {stale && <p role="alert">Processing status could not be refreshed. Displayed information may be out of date; refresh before retrying.</p>}
    <dl className="job-facts"><div><dt>Readable content</dt><dd>{status.content_ready ? 'Ready' : 'Not ready'}</dd></div><div><dt>Job analysis</dt><dd>{status.analysis_ready ? 'Ready' : 'Not ready'}</dd></div>
      <div><dt>Evidence search</dt><dd>{status.embeddings_ready ? 'Ready' : 'Not ready'}</dd></div></dl>
    {!status.stages.length && <p>Processing stages are unavailable. Refresh to check their status.</p>}
    <ol className="job-processing-stages">{status.stages.map(stage => <li key={stage.stage}>
      <div><h4>{stages[stage.stage]}</h4><Badge tone={stage.state === 'failed' ? 'danger' : 'neutral'}>{processingStates[stage.state]}</Badge>
        <p className="job-note">Updated: {stage.updated_at ? <time dateTime={stage.updated_at}>{timestamp(stage.updated_at, 'Time unknown')}</time> : 'Time unknown'}</p></div>
      {stage.state === 'degraded' && <p>Partly ready. Some evidence is incomplete or unknown; available content is kept.</p>}
      {(stage.reason || stage.state === 'unsupported' || stage.state === 'failed') && <p>{processingHelp(stage.reason ?? stage.state)}</p>}
      {stage.retryable && <Button tone="secondary" disabled={stale || query.isFetching || mutation.isPending || status.retries_remaining === 0 || stage.generation === null}
        loading={mutation.isPending && mutation.variables.stage === stage.stage} loadingLabel="Requesting retry…" onClick={() => { retry(stage); }}>Retry {stages[stage.stage].toLowerCase()}</Button>}
    </li>)}</ol>
    <p>{status.retries_remaining} processing retry requests remain in the rolling 24-hour allowance. Shared processing may already be retried by another discovery; retries reuse that attempt.</p>
    {status.retries_remaining === 0 && <p role="status">Processing retry allowance exhausted. Refresh later to check availability.</p>}
    {mutation.isSuccess && <p role="status">Retry accepted. Processing status will update while work is active.</p>}
    {error != null && <p role="alert">{error instanceof ApiError && error.status === 429 ? 'Processing retry allowance exhausted. Refresh later.'
      : error instanceof ApiError && [404, 409].includes(error.status) ? 'The source or processing attempt changed. Refresh its status before retrying.'
      : 'Retry could not be confirmed. Retrying the same stage reuses the request without another allowance charge.'}</p>}
  </section>;
}
