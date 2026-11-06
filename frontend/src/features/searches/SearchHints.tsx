import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { components } from '../../../generated/api';
import { api, ApiError } from '../../shared/api';
import { Button, Field } from '../../shared/ui';

type Definition = components['schemas']['SearchDefinition'];
type Preview = components['schemas']['SearchPreview'];
type Outcome = { key: string; value?: Preview; error?: unknown };
const languageNames = new Intl.DisplayNames(['en'], { type: 'language' });
const countryNames = new Intl.DisplayNames(['en'], { type: 'region' });

export function SearchHints({ definition, complete, days, changeDays, change, confirmed }: {
  definition: Definition; complete: boolean; days: string; changeDays: (value: string) => void;
  change: (value: Partial<Definition>) => void; confirmed: (key: string) => void;
}) {
  const options = useQuery({ queryKey: ['search-hints'], staleTime: Infinity,
    queryFn: ({ signal }) => api.request<components['schemas']['SearchHintOptions']>('/api/v1/searches/hints', { signal }) });
  const [outcome, setOutcome] = useState<Outcome | null>(null), [attempt, setAttempt] = useState(0);
  const key = JSON.stringify(definition), sites = definition.sites ?? [];
  const validDays = days === '' || /^\d+$/u.test(days) && Number(days) >= 1 && Number(days) <= 365;
  const supported = Boolean(options.data && !options.isError && validDays
    && (!definition.language || options.data.language_codes.includes(definition.language))
    && (!definition.country || options.data.country_codes.includes(definition.country))
    && sites.length <= 4 && sites.every(site => options.data.allowed_domains.includes(site))
    && (!definition.location || definition.location.trim() && Array.from(definition.location).length <= 120));
  useEffect(() => {
    if (options.error instanceof ApiError && [401, 403].includes(options.error.status)) void api.invalidateSession();
  }, [options.error]);
  useEffect(() => {
    if (!complete || !supported) { confirmed(''); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void api.request<Preview>('/api/v1/searches/preview', { method: 'POST', body: JSON.parse(key) as Definition, signal: controller.signal })
        .then(value => { if (!controller.signal.aborted) { setOutcome({ key, value }); confirmed(key); } })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setOutcome({ key, error }); confirmed('');
          if (error instanceof ApiError && [401, 403].includes(error.status)) void api.invalidateSession();
        });
    }, 300);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [key, complete, supported, attempt, confirmed]);
  const current = complete && supported && outcome?.key === key ? outcome : null;
  function retry() { setOutcome(null); confirmed(''); setAttempt(value => value + 1); }
  return <>
    <fieldset className="search-group search-fields" disabled={!options.data || options.isError}>
      <legend>Discovery hints (optional)</legend>
      <p>Google may rewrite or ignore these hints. They are separate from filters on verified job details.</p>
      <p id="search-origin-help">Country and region bias the search origin, not job location, remote eligibility or work authorization.</p>
      {(['country', 'language'] as const).map(kind => {
        const codes = kind === 'country' ? options.data?.country_codes : options.data?.language_codes;
        const selected = definition[kind] ?? '';
        return <div className="ui-field" key={kind}>
          <label htmlFor={`search-${kind}`}>{kind === 'country' ? 'Search origin country' : 'Result language'}</label>
          <select id={`search-${kind}`} className="ui-input" value={selected} aria-describedby={kind === 'country' ? 'search-origin-help' : 'search-language-help'}
            onChange={event => { change({ [kind]: event.target.value || null }); }}>
            <option value="">Unspecified — provider default</option>
            {selected && !codes?.includes(selected) && <option value={selected}>Unavailable: {selected}</option>}
            {codes?.map(code => <option key={code} value={code}>{(kind === 'country' ? countryNames.of(code.toUpperCase()) : languageNames.of(code)) ?? code} ({code})</option>)}
          </select>
        </div>;
      })}
      <p id="search-language-help">Language concerns search results, not a job's required languages.</p>
      <Field id="search-location" label="Search origin region or city" value={definition.location ?? ''} maxLength={120}
        description="Optional. For example, Paris, France. Empty uses the provider default. Use literal words without quotes or operators."
        onChange={event => { change({ location: event.target.value || null }); }} />
      <Field id="search-recency" label="Document recency in days" value={days} inputMode="numeric" maxLength={3}
        description="Optional, 1–365 days. Uses Google's document dates, not a verified posting date. Empty applies no date hint."
        {...(!validDays ? { error: 'Enter a whole number from 1 to 365, or leave empty.' } : {})} onChange={event => { changeDays(event.target.value); }} />
      <fieldset className="search-group"><legend>Domains: match any selected site (up to 4)</legend>
        <p>Nothing selected means no site restriction. Only configured domains are available; this is a search hint.</p>
        {options.data?.allowed_domains.length === 0 && <p>No domain hints are configured.</p>}
        {Array.from(new Set([...(options.data?.allowed_domains ?? []), ...sites])).map(site => <label className="proposal-choice" key={site}>
          <input type="checkbox" checked={sites.includes(site)} disabled={!sites.includes(site) && sites.length >= 4}
            onChange={event => { change({ sites: event.target.checked ? [...sites, site] : sites.filter(value => value !== site) }); }} />
          {site}{!options.data?.allowed_domains.includes(site) && ' (unavailable; remove to continue)'}
        </label>)}
      </fieldset>
      <Button tone="secondary" onClick={() => { changeDays(''); change({ country: null, language: null, location: null, sites: [] }); }}>Clear discovery hints</Button>
    </fieldset>
    {!options.data && !options.isError && <p role="status">Loading supported discovery hints…</p>}
    {options.isError && <div role="alert">Discovery hints could not be loaded. Your draft is kept.
      <Button loading={options.isFetching} onClick={() => { void options.refetch(); }}>Reload discovery hints</Button></div>}
    <section className="search-preview" aria-labelledby="search-preview-title">
      <h3 id="search-preview-title">Google query preview</h3>
      {!complete ? <p role="status">Complete each term and exclusion to preview the search.</p>
        : !supported ? <p role="status">Choose supported hints and correct invalid values to preview.</p>
          : current?.error ? <div role="alert"><p>{current.error instanceof ApiError && current.error.status === 422
            ? 'This definition is invalid. Use literal phrases, supported hints and bounded values; a query must fit within 2,048 bytes.'
            : 'The preview could not be loaded. Your draft is kept.'}</p>
            {!(current.error instanceof ApiError && current.error.status === 422) && <Button tone="secondary" onClick={retry}>Retry preview</Button>}</div>
            : !current?.value ? <p role="status">Preparing query preview…</p>
              : <><pre tabIndex={0} aria-label="Exact Google query">{current.value.query}</pre>
                <h4>Separate provider parameters</h4>
                {Object.keys(current.value.provider_parameters).length === 0 ? <p>None specified — provider defaults apply.</p>
                  : <dl>{Object.entries(current.value.provider_parameters).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>}
                {(current.value.hints ?? []).map((hint, index) => <p key={index}>{hint}</p>)}
              </>}
    </section>
  </>;
}
