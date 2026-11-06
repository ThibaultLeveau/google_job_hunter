import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, Navigate, useLocation } from 'react-router';
import type { components } from '../../../generated/api';
import { api, ApiError, sessionKey, type Session } from '../../shared/api';
import { Button, Field, StatePanel } from '../../shared/ui';
import { useAuthAction } from '../auth/useAuthAction';

type Action = components['schemas']['ActionRequest']['action'];
type Operation = 'link' | 'export' | 'delete' | 'password' | 'google';
const notices = {
  linked: 'Google returned. Check your linked sign-in methods below.',
  reauthenticated: 'Authentication returned. Try your action again to continue.',
  conflict: 'Google could not be linked. That identity may belong to another account.',
  cancelled: 'Google was cancelled. No further action was submitted.',
  reauthentication_required: 'Authentication is too old. Confirm your identity and try again.',
  failed: 'Google did not complete. Check your connection and try again.',
};

export function AccountCallback({ children }: { children: ReactNode }) {
  const location = useLocation();
  const params = new URLSearchParams(location.hash.slice(1));
  const values = [...params.getAll('google_link'), ...params.getAll('google_reauthenticate')];
  if (!values.length) return children;
  const outcome = values.length === 1 && Object.hasOwn(notices, values[0] ?? '') ? values[0] : 'failed';
  return <Navigate replace to="/settings" state={{ accountNotice: outcome }} />;
}

function DeletionDialog({ busy, close, submit }: { busy: boolean; close: () => void; submit: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null), cancel = useRef<HTMLButtonElement>(null);
  const [intent, setIntent] = useState('');
  useEffect(() => {
    const element = dialog.current, previous = document.activeElement;
    element?.showModal(); cancel.current?.focus();
    return () => { element?.close(); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  return <dialog ref={dialog} tabIndex={-1} className="settings-dialog" aria-labelledby="delete-title" aria-describedby="delete-help"
    onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)')];
      const first = controls[0], last = controls.at(-1);
      if (!first || (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
        event.preventDefault(); (event.shiftKey ? last : first)?.focus();
        if (!first) event.currentTarget.focus();
      }
    }}
    onCancel={event => { event.preventDefault(); if (!busy) close(); }}>
    <h2 id="delete-title">Permanently delete your account?</h2>
    <p id="delete-help">This cannot be undone. You will be signed out immediately. Your documents, profile, searches and private matches will be queued for deletion. Shared public job facts remain.</p>
    <form onSubmit={event => { event.preventDefault(); if (intent === 'DELETE' && !busy) submit(); }}>
      <Field label="Type DELETE to confirm" value={intent} onChange={event => { setIntent(event.target.value); }} autoComplete="off" disabled={busy} />
      <div className="settings-actions">
        <Button ref={cancel} tone="secondary" disabled={busy} onClick={close}>Cancel</Button>
        <Button type="submit" tone="danger" disabled={intent !== 'DELETE'} loading={busy} loadingLabel="Deleting account…">Permanently delete account</Button>
      </div>
    </form>
  </dialog>;
}

export function AccountSettings({ user, deleted }: { user: Session; deleted: () => void }) {
  const location = useLocation();
  const state: unknown = location.state;
  const returned = typeof state === 'object' && state !== null && 'accountNotice' in state && typeof state.accountNotice === 'string'
    && Object.hasOwn(notices, state.accountNotice) ? notices[state.accountNotice as keyof typeof notices] : '';
  const [message, setMessage] = useState(returned), [failed, setFailed] = useState(false);
  const [confirm, setConfirm] = useState(false), [proof, setProof] = useState<Action | null>(null);
  const [authorization, setAuthorization] = useState('');
  const [passwordRejected, setPasswordRejected] = useState(false);
  const notice = useRef<HTMLParagraphElement>(null), password = useRef<HTMLInputElement>(null), googleLink = useRef<HTMLAnchorElement>(null);
  const operation = useRef<Operation>('export'), mounted = useRef(true), inFlight = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const current = () => {
    const session = api.queryClient.getQueryData<Session | null>(sessionKey);
    return mounted.current && session?.id === user.id && session.csrf_token === user.csrf_token;
  };
  const methods = useQuery({ queryKey: ['account-methods', user.id], retry: false,
    queryFn: ({ signal }) => api.request<components['schemas']['MethodStatus']>('/api/auth/methods', { signal }) });
  const providers = useQuery({ queryKey: ['auth-providers'], retry: false,
    queryFn: ({ signal }) => api.request<{ google: boolean }>('/api/auth/providers', { signal }) });
  useEffect(() => {
    if (methods.error instanceof ApiError && methods.error.status === 401) api.clearSession();
  }, [methods.error]);
  useEffect(() => { if (message) notice.current?.focus(); }, [message]);
  useEffect(() => { if (proof) password.current?.focus(); }, [proof]);
  useEffect(() => { if (authorization) googleLink.current?.focus(); }, [authorization]);

  const action = useAuthAction(async () => {
    const op = operation.current;
    const intent: Action = op === 'link' ? 'link_google' : op === 'delete' ? 'delete_account' : op === 'export' ? 'export_data' : proof ?? 'settings';
    try {
      if (op === 'password') {
        const value = password.current?.value ?? '';
        if (password.current) password.current.value = '';
        await api.request('/api/auth/reauthenticate/password', { method: 'POST', body: { action: intent, password: value } });
        if (!current()) return;
        setProof(null); setMessage('Identity confirmed. Try your action again.'); void methods.refetch();
      } else if (op === 'link' || op === 'google') {
        const result = await api.request<components['schemas']['Authorization']>(`/api/auth/google/${op === 'link' ? 'link' : 'reauthenticate'}`, { method: 'POST', body: { action: intent } });
        if (!current()) return;
        const url = new URL(result.authorization_url);
        if (url.origin !== 'https://accounts.google.com' || url.username || url.password) throw new Error('Invalid authorization origin');
        setAuthorization(url.href);
      } else if (op === 'export') {
        const data = await api.request<components['schemas']['PersonalExport']>('/api/v1/account/export', { method: 'POST' });
        if (!current()) return;
        const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
        const link = document.createElement('a'); link.href = url; link.download = 'job-hunter-personal-data.json';
        document.body.append(link);
        try { link.click(); } finally { link.remove(); window.setTimeout(() => { URL.revokeObjectURL(url); }, 1000); }
        setMessage('Your data download has started. Keep this personal file somewhere private.');
      } else {
        await api.request('/api/v1/account/delete', { method: 'POST', body: { intent: 'delete_account' } });
        if (current()) deleted();
      }
    } catch (error) {
      if (!current()) return;
      setConfirm(false); setFailed(true);
      if (error instanceof ApiError && error.status === 403 && error.detail?.code === 'reauthentication_required') {
        setProof(intent); setMessage('Confirm your identity before trying this action again.');
      } else if (error instanceof ApiError && error.status === 401) {
        if (op !== 'password') { api.clearSession(); return; }
        setPasswordRejected(true); setMessage('Your password was not accepted. Try again.'); void api.invalidateSession();
      } else setMessage(error instanceof ApiError && error.status === 429 ? 'Too many attempts. Wait before trying again.'
        : error instanceof ApiError && error.status === 413 ? 'This export is too large. Contact the service operator for help.'
        : error instanceof ApiError && error.status === 503 ? 'This service is unavailable. Try again later.'
        : op === 'delete' ? 'Deletion could not be confirmed. Reload to check your session before trying again.'
        : 'The request did not complete. Check your connection and try again.');
      throw error;
    } finally { inFlight.current = false; }
  });
  const busy = action.isPending, blocked = busy || action.wait > 0;
  function run(op: Operation) {
    if (blocked || inFlight.current) return;
    inFlight.current = true; operation.current = op; setPasswordRejected(false); setMessage(''); setFailed(false); setAuthorization(''); action.run();
  }
  if (methods.isPending) return <StatePanel state="loading" title="Loading account settings" description="Checking your linked sign-in methods." />;
  if (methods.isError) return <StatePanel state="error" title="Account settings could not be loaded" description="Try again to check your sign-in methods."
    action={<Button loading={methods.isFetching} onClick={() => { void methods.refetch(); }}>Retry account settings</Button>} />;
  return <div className="settings-content">
    {message && <p id="settings-notice" ref={notice} tabIndex={-1} role={failed ? 'alert' : 'status'}>{message}</p>}
    {action.wait > 0 && <p role="status">Try again in {action.wait} seconds.</p>}
    <section aria-labelledby="identity-title" className="settings-panel">
      <h2 id="identity-title">Sign-in and verification</h2>
      <p>{user.email}</p><p>{user.email_verified ? 'Your email is verified.' : 'Your email is not verified.'}</p>
      {!user.email_verified && <Link to="/verify-email">Open email verification</Link>}
      <ul><li>Password: {methods.data.methods.includes('password') ? 'available' : 'not set'}</li>
        <li>Google: {methods.data.methods.includes('google') ? 'linked' : 'not linked'}</li></ul>
      {!methods.data.methods.length && <p>Sign-in methods are unknown. Reload to check your account.</p>}
      <p>Google connects only when you choose to link it. Matching email addresses never merge accounts.</p>
      {providers.isPending ? <p role="status">Checking Google availability…</p> : providers.isError ? <>
        <p role="alert">Google availability could not be checked.</p><Button tone="secondary" onClick={() => { void providers.refetch(); }}>Retry Google availability</Button>
      </> : !providers.data.google ? <p>Google is unavailable on this service.</p>
        : !methods.data.methods.includes('google') && <Button tone="secondary" disabled={blocked} onClick={() => { run('link'); }}>Link Google</Button>}
    </section>
    {proof && <section aria-labelledby="proof-title" className="settings-panel">
      <h2 id="proof-title">Confirm your identity</h2><p>Use an existing sign-in method. Your action will need to be submitted again afterward.</p>
      {methods.data.methods.includes('password') && <form className="auth-content" onSubmit={event => { event.preventDefault(); run('password'); }}>
        <Field ref={password} type="password" label="Current password" autoComplete="current-password" maxLength={128} required disabled={blocked}
          aria-invalid={passwordRejected || undefined} aria-describedby={passwordRejected ? 'settings-notice' : undefined} />
        <Button type="submit" disabled={blocked}>Confirm with password</Button>
      </form>}
      {methods.data.methods.includes('google') && providers.data?.google && <Button disabled={blocked} onClick={() => { run('google'); }}>Confirm with Google</Button>}
      <Button tone="secondary" disabled={busy} onClick={() => { setProof(null); setAuthorization(''); setMessage('Identity confirmation cancelled.'); }}>Cancel identity confirmation</Button>
    </section>}
    {authorization && <p><a ref={googleLink} className="ui-button" href={authorization}>Continue to Google</a></p>}
    <section aria-labelledby="data-title" className="settings-panel">
      <h2 id="data-title">Your data</h2>
      <p>Download your profile, document text and research as JSON. Original uploads are available from your profile.</p>
      <Button disabled={blocked} onClick={() => { run('export'); }}>Download personal data</Button>
      <p>Uploaded text is parsed to propose profile facts. Configured AI services may process text to produce suggestions and job matches. Review suggestions before accepting them.</p>
      <p>Retention is configured by the service operator. Encrypted backups and external providers have separate retention policies; deleting your account does not erase their copies immediately.</p>
      <Link to="/profile">Manage or remove an individual upload</Link>
    </section>
    <section aria-labelledby="deletion-title" className="settings-panel">
      <h2 id="deletion-title">Delete account</h2>
      <p>Delete your private workspace and sign out all sessions. Cleanup can take time and may be retried. Download anything you want to keep first.</p>
      <Button tone="danger" disabled={blocked} onClick={() => { setConfirm(true); }}>Delete my account</Button>
    </section>
    {busy && !confirm && <p role="status">Working on your request…</p>}
    {confirm && <DeletionDialog busy={busy} close={() => { setConfirm(false); }} submit={() => { run('delete'); }} />}
  </div>;
}
