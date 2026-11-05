import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useNavigate } from 'react-router';
import type { components } from '../../../generated/api';
import { api, ApiError, sessionKey, type Session } from '../../shared/api';
import { Button, StatePanel } from '../../shared/ui';
import { EmailAuth } from './EmailAuth';
import { authError, useAuthAction } from './useAuthAction';

function ResendVerification() {
  const action = useAuthAction(() => api.request<components['schemas']['RegistrationResult']>(
    '/api/auth/resend-verification', { method: 'POST' }).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 401) api.clearSession();
      throw error;
    }));
  return <div className="auth-content">
    <Button tone="secondary" loading={action.isPending} disabled={action.wait > 0} onClick={action.run}
      loadingLabel="Requesting email…">Send a new verification email</Button>
    {action.isSuccess && <p role="status">Request received. Check your inbox for a new link. Your email is not verified yet.</p>}
    {action.isError && <p role="alert">{authError(action.error)}</p>}
    {action.wait > 0 && <p role="status">Try again in {action.wait} seconds.</p>}
  </div>;
}

export function VerificationRequired() {
  return <StatePanel state="unknown" title="Verify your email to continue"
    description="Verify your email before using profile, search and job tools. Account settings and sign-out remain available."
    action={<Link to="/verify-email">Open email verification</Link>} />;
}

export function Verification() {
  const location = useLocation();
  const navigate = useNavigate();
  const session = useQuery(api.currentSession);
  const outcome = useRef<HTMLParagraphElement>(null);
  function readLink() {
    const tokens = new URLSearchParams(location.hash.slice(1)).getAll('token');
    const token = tokens.length === 1 && /^[A-Za-z0-9_-]{43}$/.test(tokens[0] ?? '') ? tokens[0] : null;
    return { key: location.key, token };
  }
  const [link, setLink] = useState(readLink);
  if (location.key !== link.key) setLink(location.hash || location.search ? readLink() : { ...link, key: location.key });
  useEffect(() => {
    if (location.hash || location.search) void navigate('/verify-email', { replace: true });
  }, [location.hash, location.search, navigate]);
  const action = useAuthAction(async () => {
    const token = link.token; setLink({ ...link, token: null });
    if (!token) throw new Error('Missing verification link.');
    const body: components['schemas']['VerificationRequest'] = { token };
    try {
      await api.request<components['schemas']['RegistrationResult']>('/api/auth/verify', { method: 'POST', body });
      await api.queryClient.invalidateQueries({ queryKey: sessionKey, exact: true }, { throwOnError: true });
      return api.queryClient.getQueryData<Session | null>(sessionKey)?.email_verified === true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) api.clearSession();
      throw error;
    }
  });
  useEffect(() => {
    if (action.isSuccess || action.isError) outcome.current?.focus();
  }, [action.isSuccess, action.isError, session.data?.email_verified]);
  if (session.isError) return <StatePanel state="error" title="We couldn’t check your account"
    description="Try again to check whether your email is verified."
    action={<Button loading={session.isFetching} onClick={() => { void session.refetch(); }}>Try again</Button>} />;
  if (session.isPending) return <StatePanel state="loading" title="Checking your account" description="Please wait for your session." />;
  if (!session.data) return <div className="auth-content">
    <p>Sign in to the account for this verification email. Keep this page open while signing in.</p>
    <EmailAuth mode="login" />
  </div>;
  if (session.data.email_verified) return <section className="ui-state auth-content" aria-label="Email verified">
    <p ref={outcome} tabIndex={-1} role="status">Your account email is verified.</p><Link to="/profile">Continue to your profile</Link>
  </section>;
  return <section className="ui-state auth-content" aria-label="Verify your email">
    <p>Signed in as <strong>{session.data.email}</strong>. Your email is not verified yet.</p>
    {link.token && <Button onClick={action.run} disabled={action.wait > 0}>Verify email</Button>}
    {action.isPending && <p role="status">Checking your verification link…</p>}
    {action.isSuccess && !action.data && <p ref={outcome} tabIndex={-1} role="alert">This link could not verify your email. It may be expired, already used or for another account.</p>}
    {action.isError && <p ref={outcome} tabIndex={-1} role="alert">{authError(action.error)} Reopen your email link or request a new one.</p>}
    {action.wait > 0 && <p role="status">Try again in {action.wait} seconds.</p>}
    {!link.token && !action.isPending && <p>Open the link from your latest verification email. Links last one hour; sending a new email replaces older links.</p>}
    <ResendVerification />
  </section>;
}
