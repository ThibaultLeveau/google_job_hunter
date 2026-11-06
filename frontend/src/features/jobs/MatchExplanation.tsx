import { useRef, type MouseEvent } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Link } from 'react-router';
import type { components } from '../../../generated/api';
import { api, ApiError, sessionKey, type Session } from '../../shared/api';
import { Badge, Button } from '../../shared/ui';

type Detail = components['schemas']['JobDetail'];
type Accepted = components['schemas']['WorkAccepted'];
const labels = { matched: 'Matched', partial: 'Partial', missing: 'Missing', unknown: 'Unknown' };
const priorities = { required: 'Required', preferred: 'Preferred', unclear: 'Priority unclear' };
function reveal(event: MouseEvent<HTMLAnchorElement>, id: string) {
  const target = document.getElementById(id);
  if (!target) return;
  event.preventDefault();
  let parent = target.closest('details');
  while (parent) { parent.open = true; parent = parent.parentElement?.closest('details') ?? null; }
  target.focus();
}
function evidenceLink(id: string, label: string) {
  return <a href={`#${id}`} onClick={event => { reveal(event, id); }}>{label}</a>;
}

export function MatchExplanation({ data, ownerId, queryId, unavailable, refreshing }: { data: Detail; ownerId: string; queryId: string; unavailable: boolean; refreshing: boolean }) {
  const { status, match, match_context: context } = data;
  const key = useRef<string | null>(null), busy = useRef(false);
  const queryKey = ['job-detail', ownerId, queryId];
  const activeOwner = () => api.queryClient.getQueryData<Session | null>(sessionKey)?.id === ownerId;
  const mutation = useMutation({
    mutationFn: (idempotency_key: string) => api.request<Accepted>(`/api/v1/jobs/${encodeURIComponent(status.id)}/reevaluate`, { method: 'POST', body: { idempotency_key } }),
    onSuccess: async () => {
      if (!activeOwner()) return;
      key.current = null;
      api.queryClient.setQueryData<Detail>(queryKey, old => old && ({ ...old, status: { ...old.status,
        match_current: false, match_stale: Boolean(old.match), match_pending: true, match_state: old.match ? 'stale' : 'pending' } }));
      await Promise.all([api.queryClient.invalidateQueries({ queryKey, exact: true }), api.queryClient.invalidateQueries({ queryKey: ['jobs', ownerId] })]);
    },
    onError: (error: unknown) => {
      if (!activeOwner()) return;
      if (error instanceof ApiError && [401, 403].includes(error.status)) void api.invalidateSession();
      else if (error instanceof ApiError && [404, 409, 429].includes(error.status)) void api.queryClient.invalidateQueries({ queryKey, exact: true });
    },
    onSettled: () => { busy.current = false; },
  });
  const blocked = refreshing || mutation.isPending || status.match_pending || unavailable || !status.analysis_ready || status.reevaluations_remaining === 0;
  function reevaluate() {
    if (busy.current || blocked) return;
    busy.current = true; key.current ??= crypto.randomUUID(); mutation.mutate(key.current);
  }
  const error = mutation.error;
  const visible = match && (status.match_current || status.match_stale) && ['ready', 'unknown'].includes(match.state);
  const sourceEvidence = context ? Object.entries(context.citations).filter(([id]) => match?.evaluations.some(item => item.requirement_evidence.includes(id))) : [];
  return <section className="job-detail-panel" aria-labelledby="job-match-heading"><h3 id="job-match-heading">Your profile match</h3>
    <Badge tone={status.match_state === 'failed' ? 'danger' : 'neutral'}>{status.match_stale ? 'Outdated result' : status.match_state === 'failed' ? 'Evaluation failed' : status.match_current ? 'Current evaluation' : 'Evaluation pending'}</Badge>
    <p>{status.match_stale ? 'An earlier profile evaluation is out of date.' : status.match_current ? 'A profile evaluation is available.'
      : status.match_state === 'failed' || unavailable ? 'Profile evaluation is unavailable.' : 'Profile evaluation is pending.'}</p>
    {status.match_stale && <p>{context?.profile_changed ? 'Your accepted profile changed after this evaluation. The evidence below belongs to the earlier profile.'
      : 'A newer evaluation is needed. The evidence below belongs to the earlier result.'}</p>}
    {status.match_state === 'failed' && <p role="status">The latest evaluation failed. {status.match_reason === 'quota' || match?.reason === 'quota' ? 'Provider capacity is exhausted; try again after capacity resets.' : 'Available earlier evidence remains below.'}</p>}
    {status.match_pending && !unavailable && <p role="status">{status.analysis_ready ? 'Evaluation is pending. Refresh details to check for completion.' : 'Evaluation is waiting for job analysis.'}</p>}
    {visible && <>
      <dl className="job-facts"><div><dt>{status.match_stale ? 'Outdated match score' : 'Match score'}</dt><dd>{match.score.percent === null ? 'Score unknown' : `${String(match.score.percent)}%`}</dd></div>
        <div><dt>Evidence coverage</dt><dd>{match.score.assessed_percent === null ? 'Coverage unknown' : `${String(match.score.assessed_percent)}% of weighted job requirements assessed`}</dd></div></dl>
      <p>{match.score.unknown_count} of {match.score.requirement_count} requirement assessments are unknown. Unknown evidence is not a confirmed gap.</p>
      <p>A match score summarizes assessed evidence; it does not guarantee eligibility or hiring. Missing means no supporting accepted fact was identified, not that you lack the qualification.</p>
      {!match.retrieval_complete && <p role="status">Evidence retrieval was incomplete. Some assessments may remain unknown.</p>}
      {!match.evaluations.length ? <p>No requirement assessments are available. This does not mean no requirements apply.</p>
        : <details className="job-disclosure"><summary>Requirement assessments ({match.evaluations.length})</summary>
          {(Object.keys(labels) as (keyof typeof labels)[]).map(state => {
            const items = match.evaluations.filter(item => item.status === state);
            return items.length > 0 && <section key={state} aria-labelledby={`match-${state}`}><h4 id={`match-${state}`}>{labels[state]} ({items.length})</h4>
              <ul className="job-requirements">{items.map(item => {
                const requirement = context?.requirements.items.find(value => value.id === item.requirement_id);
                const component = match.score.components.find(value => value.requirement_id === item.requirement_id);
                return <li key={item.requirement_id}><p className="job-text">{requirement?.text ?? 'Requirement text unavailable for this evaluation.'}</p>
                  {requirement && <Badge>{priorities[requirement.level]}</Badge>}<p className="job-text">{item.rationale}</p>
                  {component && <p>{component.weight === 0 ? 'Application checklist item; excluded from the match score.' : component.status === 'unknown' ? 'Unknown evidence; excluded from assessed score.'
                    : `Score contribution: ${String(component.earned_units)} of ${String(component.possible_units)} points · Weight ${String(component.weight)}`}</p>}
                  <p>Job evidence: {item.requirement_evidence.map((id, index) => <span key={id}>{index > 0 && ' · '}{context?.citations[id]
                    ? evidenceLink(`match-source-${id}`, `Source excerpt ${String(index + 1)}`) : 'Source excerpt unavailable'}</span>)}</p>
                  <p>Profile evidence: {item.profile_evidence.length ? item.profile_evidence.map((id, index) => {
                    const fact = context?.profile_facts.find(value => value.id === id);
                    return <span key={id}>{index > 0 && ' · '}{fact ? evidenceLink(`match-profile-${id}`, fact.skill ?? fact.title ?? 'Accepted project description') : 'Accepted fact unavailable'}</span>;
                  }) : 'No explicit accepted facts linked.'}</p>
                </li>;
              })}</ul></section>;
          })}
        </details>}
      {!context && <p>Historical profile and source evidence is unavailable. Current facts are not substituted.</p>}
      {context && <>
        <details className="job-disclosure"><summary>Profile facts used as evidence ({context.profile_facts.length})</summary>
          <p>Accepted professional facts at profile revision {match.profile_revision}. <Link to="/profile">Review your current profile</Link></p>
          {!context.profile_facts.length && <p>No explicit accepted facts were linked to this evaluation.</p>}
          <ul className="job-requirements">{context.profile_facts.map(fact => <li key={fact.id} id={`match-profile-${fact.id}`} tabIndex={-1}>
            <p className="job-text">{fact.kind === 'skill' ? fact.skill ?? 'Skill unknown' : fact.kind === 'experience' ? fact.title ?? 'Experience title unknown' : 'Accepted project'}</p>
            {fact.kind !== 'skill' && <p className="job-text">{fact.description ?? 'Description not provided.'}</p>}
          </li>)}</ul>
        </details>
        <details className="job-disclosure"><summary>Job source used as evidence ({sourceEvidence.length})</summary>
          <p>Preserved excerpts from the source version evaluated. These may differ from the current posting above.</p>
          <div className="job-evidence">{sourceEvidence.map(([id, span]) => {
            const paragraph = context.source.paragraphs?.findIndex(value => value.id === span.paragraph_id) ?? -1;
            return <figure key={id} id={`match-source-${id}`} tabIndex={-1}><blockquote>{span.quote}</blockquote>
              <figcaption>{paragraph < 0 ? 'Source paragraph unavailable' : `Source paragraph ${String(paragraph + 1)}`} · Characters {span.start}–{span.end}</figcaption></figure>;
          })}</div>
        </details>
      </>}
    </>}
    <p>{status.reevaluations_remaining} reevaluation requests remain in the rolling 24-hour allowance. Provider capacity also applies.</p>
    {status.reevaluations_remaining === 0 && <p role="status">Reevaluation allowance exhausted. Refresh details later to check availability.</p>}
    {mutation.isSuccess && <p role="status">Reevaluation requested. Refresh details to check the result.</p>}
    {error != null && <p role="alert">{error instanceof ApiError && error.status === 429 ? 'Reevaluation allowance exhausted. Refresh details later.'
      : error instanceof ApiError && [404, 409].includes(error.status) ? 'Job evidence or access changed. Refresh details before trying again.'
      : 'Reevaluation request could not be confirmed. Retry uses the same request to avoid spending another allowance.'}</p>}
    <Button tone="secondary" disabled={blocked} loading={mutation.isPending} loadingLabel="Requesting reevaluation…" onClick={reevaluate}>{mutation.isError ? 'Retry reevaluation request' : 'Reevaluate profile match'}</Button>
  </section>;
}
