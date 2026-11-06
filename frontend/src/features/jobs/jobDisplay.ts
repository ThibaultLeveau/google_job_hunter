import type { components } from '../../../generated/api';

type Job = components['schemas']['JobSummary'];
export const arrangements: Record<Job['remote'], string> = {
  remote: 'Remote', hybrid: 'Hybrid', on_site: 'On site', unknown: 'Work arrangement unknown', conflicting: 'Conflicting work arrangements',
};
export function fact(value: Job['title'], unknown: string) {
  if (!value.status || value.status === 'unknown' || !value.values?.length) return unknown;
  return `${value.values.join(' · ')}${value.status === 'known' ? '' : ' (uncertain)'}`;
}
export function salary(value: Job['salary']) {
  if (!value.status || value.status === 'unknown' || !value.values?.length) return 'Salary unknown';
  return value.values.map(item => {
    const amount = item.minimum === null ? item.maximum === null ? 'Amount unknown' : `Up to ${item.maximum}`
      : item.maximum === null ? `From ${item.minimum}` : item.minimum === item.maximum ? item.minimum : `${item.minimum}–${item.maximum}`;
    return `${item.currency ?? 'Currency unknown'} ${amount} / ${item.period ?? 'period unknown'} · ${item.basis ?? 'basis unknown'}${item.uncertain ? ' (uncertain)' : ''}`;
  }).join('; ') + (value.status === 'known' ? '' : ' — conflicting or uncertain salary information');
}
export function sourceLink(value: string) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url : null; }
  catch { return null; }
}
export function timestamp(value: string, unknown = 'Fetch time unknown') {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? unknown : date.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}
