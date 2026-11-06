export const sortLabels = { newest: 'Newest discoveries first', oldest: 'Oldest discoveries first', title: 'Job title A–Z', match: 'Highest match first' };
export const choices = {
  sort: sortLabels, remote: { remote: 'Remote', hybrid: 'Hybrid', on_site: 'On site' },
  match_status: { current: 'Current', stale: 'Stale', pending: 'Pending', failed: 'Failed', unknown: 'Score unknown' },
  period: { hour: 'Hour', day: 'Day', week: 'Week', month: 'Month', year: 'Year' },
};
const fields = ['keyword', 'run_id', 'region', 'remote', 'match_status', 'salary_min', 'salary_max', 'currency', 'period', 'sort',
  'include_unknown_region', 'include_unknown_remote', 'include_unknown_salary'] as const;
export type Filter = typeof fields[number];
export type Filters = Record<Filter, string>;
export function readFilters(params: URLSearchParams): Filters {
  return Object.fromEntries(fields.map(key => [key, params.get(key) ?? ''])) as Filters;
}
export function filterParams(values: Filters) {
  return new URLSearchParams(fields.filter(key => values[key] && values[key] !== 'false' && !(key === 'sort' && values[key] === 'newest')).map(key => [key, values[key]]));
}
function amount(value: string) {
  const [whole = '0', fraction = ''] = value.split('.'); return BigInt(whole + fraction.padEnd(4, '0'));
}
export function filterErrors(params: URLSearchParams) {
  const errors: Partial<Record<Filter | 'url', string>> = {}, values = readFilters(params);
  for (const [key, value] of params) {
    if (![...fields, 'cursor'].includes(key) || params.getAll(key).length > 1) errors.url = 'The link contains unsupported or repeated filters. Clear all to start again.';
    if (key === 'cursor' && value.length > 2048) errors.url = 'The results cursor is too long. Clear all to start again.';
  }
  for (const key of ['keyword', 'region'] as const) {
    if (values[key] && (!values[key].trim() || values[key].length > 80 || values[key].includes('\0'))) errors[key] = 'Use 1–80 characters, with no null characters.';
  }
  for (const key of ['sort', 'remote', 'match_status', 'period'] as const) {
    if (values[key] && !Object.hasOwn(choices[key], values[key])) errors[key] = 'Choose a supported option.';
  }
  if (values.run_id && !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(values.run_id)) errors.run_id = 'Choose a valid search run.';
  for (const key of ['include_unknown_region', 'include_unknown_remote', 'include_unknown_salary'] as const) {
    if (values[key] && !['true', 'false'].includes(values[key])) errors[key] = 'Use true or false for unknown data.';
  }
  for (const key of ['salary_min', 'salary_max'] as const) {
    if (values[key] && (!/^\d{1,13}(\.\d{1,4})?$/.test(values[key]) || Number(values[key]) > 1000000000000)) errors[key] = 'Use an amount from 0 to 1,000,000,000,000 with up to four decimal places.';
  }
  if ([values.salary_min, values.salary_max, values.currency, values.period].some(Boolean)) {
    if (!/^[A-Z]{3}$/.test(values.currency)) errors.currency = 'Enter a three-letter currency, such as EUR.';
    if (!values.period) errors.period = 'Choose the salary period.';
    if (values.salary_min && values.salary_max && !errors.salary_min && !errors.salary_max && amount(values.salary_min) > amount(values.salary_max)) errors.salary_max = 'Maximum must be at least the minimum.';
  }
  return errors;
}
