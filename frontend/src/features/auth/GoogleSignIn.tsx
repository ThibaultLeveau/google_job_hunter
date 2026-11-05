import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, Navigate, useLocation, useNavigate, matchPath } from 'react-router';
import type { paths } from '../../../generated/api';
import { api } from '../../shared/api';
import { Button, StatePanel } from '../../shared/ui';
import { safeReturnPath } from '../../app/routes';

const returnKey = 'job-hunter.google-return';
const outcomes = ['signed_in', 'cancelled', 'linking_required', 'failed'] as const;

export function GoogleSignIn() {
  const location = useLocation();
  const providers = useQuery({ queryKey: ['auth-providers'], staleTime: 60000,
    queryFn: ({ signal }) => api.request<paths['/api/auth/providers']['get']['responses'][200]['content']['application/json']>(
      '/api/auth/providers', { signal }) });
  if (providers.isPending) return <p role="status">Checking sign-in options…</p>;
  if (providers.isError) return <div>
    <p role="alert">Google sign-in availability could not be checked.</p>
    <Button tone="secondary" loading={providers.isFetching} onClick={() => { void providers.refetch(); }}>Retry Google availability</Button>
  </div>;
  if (providers.data.google !== true) return null;
  return <div className="auth-content">
    <a className="ui-button google-signin" href="/api/auth/google/start" onClick={() => {
      try { sessionStorage.setItem(returnKey, safeReturnPath(new URLSearchParams(location.search).get('returnTo'))); }
      catch { /* Storage can be unavailable; the callback then uses the default workspace. */ }
    }}>
      <svg aria-hidden="true" focusable="false" viewBox="0 0 48 48">
        <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
        <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
        <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
        <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
      </svg><span>Sign in with Google</span>
    </a>
    {matchPath('/verify-email', location.pathname) && <p>Google sign-in leaves this page. Reopen your verification email afterward.</p>}
  </div>;
}

export function GoogleCallback({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const session = useQuery({ ...api.currentSession, refetchOnMount: false });
  const handled = useRef(-1);
  const notice = useRef<HTMLDivElement>(null);
  const query = new URLSearchParams(location.search);
  const incoming = Boolean(location.hash && location.hash !== '#main-content') || ['code', 'state', 'error', 'token'].some(key => query.has(key));
  const source = location.hash + location.search;
  function readCallback() {
    const params = new URLSearchParams(location.hash.slice(1));
    const supplied = params.getAll('google');
    const outcome = supplied.length === 1 ? outcomes.find(value => value === supplied[0]) : undefined;
    let target = query.get('returnTo');
    if (incoming) {
      try { target = sessionStorage.getItem(returnKey) ?? target; } catch { /* Use the validated URL fallback. */ }
    }
    return { outcome: incoming ? outcome ?? 'failed' : null, target: safeReturnPath(target) };
  }
  const [callback, setCallback] = useState(() => ({ ...readCallback(), source, version: 0 }));
  const [checked, setChecked] = useState(-1);
  const checking = Boolean(callback.outcome) && checked !== callback.version;
  if (source !== callback.source) setCallback(incoming
    ? { ...readCallback(), source, version: callback.version + 1 }
    : { ...(callback.outcome ? callback : { ...callback, ...readCallback() }), source });
  useEffect(() => {
    if (!incoming || handled.current === callback.version) return;
    handled.current = callback.version;
    try { sessionStorage.removeItem(returnKey); } catch { /* No persisted credentials are needed. */ }
    void navigate(`/login?returnTo=${encodeURIComponent(callback.target)}`, { replace: true });
    const complete = () => { setChecked(current => Math.max(current, callback.version)); };
    void api.invalidateSession().then(complete, complete);
  }, [callback, incoming, navigate]);
  useEffect(() => {
    if (callback.outcome && !checking) notice.current?.focus();
  }, [callback.outcome, checking, session.isError]);
  if (callback.outcome === 'signed_in' && (checking || session.isPending || session.isFetching)) {
    return <StatePanel state="loading" title="Checking Google sign-in" description="Confirming your account session." />;
  }
  if (callback.outcome === 'signed_in' && session.isError) return <StatePanel state="error"
    title="We couldn’t confirm Google sign-in" description="Retry the session check before continuing."
    action={<Button loading={session.isFetching} onClick={() => { void session.refetch(); }}>Check session again</Button>} />;
  if ((!callback.outcome || callback.outcome === 'signed_in') && session.isSuccess && session.data) {
    return <Navigate replace to={callback.target} />;
  }
  const collision = callback.outcome === 'linking_required';
  return <div className="auth-content" onSubmit={() => { setCallback(current => ({ ...current, outcome: null })); }}>
    {callback.outcome && <div ref={notice} tabIndex={-1}><StatePanel state="error"
      title={collision ? 'Use your existing sign-in method' : callback.outcome === 'cancelled' ? 'Google sign-in was cancelled' : 'Google sign-in did not complete'}
      description={collision ? 'Sign in to your existing account first. Connecting Google requires a separate action in Account settings; matching email addresses never connect accounts.'
        : 'You can try again or sign in with your email and password.'}
      {...(collision ? { action: <Link to="/settings">Account settings</Link> } : {})} /></div>}
    {session.data && callback.outcome && callback.outcome !== 'signed_in'
      ? <Link to={callback.target}>Return to your workspace</Link> : children}
  </div>;
}
