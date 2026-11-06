export const processingStates = { pending: 'Pending', running: 'Processing', completed: 'Ready', degraded: 'Partly ready', unsupported: 'Unsupported', failed: 'Failed' };
export const isProcessing = (state: string) => ['queued', 'processing', 'deleting', 'waiting_for_parse', 'pending', 'running'].includes(state);

export function processingHelp(code: string | null, subject: 'Search' | 'Parsing' | 'Processing' = 'Processing') {
  if (code === 'quota') return `The ${subject.toLowerCase()} quota is exhausted. Wait for capacity or contact your workspace administrator.`;
  if (code === 'rate_limited') return `${subject} is temporarily rate limited. Wait before trying again.`;
  if (['disabled', 'unconfigured', 'authentication'].includes(code ?? '')) return `${subject} configuration needs attention. Contact your workspace administrator.`;
  if (['http_denied', 'robots_denied', 'blocked', 'interactive_challenge'].includes(code ?? '')) return 'Source access is denied. Open the original posting if available.';
  if (['unsupported', 'content_type', 'javascript_required'].includes(code ?? '')) return 'This source cannot be processed. Use the original posting when available.';
  if (['removed', 'http_removed'].includes(code ?? '')) return 'The original posting was removed. Preserved information may be out of date.';
  if (['non_job', 'non_job_marker', 'empty', 'empty_page'].includes(code ?? '')) return 'No usable job posting was found at this source.';
  if (['timed_out', 'timeout'].includes(code ?? '')) return subject === 'Parsing' ? 'Parsing timed out. Try a shorter, simpler document.' : `${subject} timed out. Try again when a retry is available.`;
  if (['input_limit', 'limits', 'limits_exceeded', 'output_limit'].includes(code ?? '')) return 'Processing limits were reached. Available information is kept; some content may remain unavailable.';
  if (['unverified_evidence', 'invalid_response', 'malformed'].includes(code ?? '')) return 'Some evidence could not be verified. Review available source text; unsupported facts remain unknown.';
  return 'Processing could not complete. Available information is kept. Retry when offered, or check again later.';
}
