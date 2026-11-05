import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import type { components } from '../../../generated/api';
import { api } from '../../shared/api';
import { Button, Field, StatePanel } from '../../shared/ui';
import { authError, fieldError, useAuthAction } from './useAuthAction';

export function Recovery({ reset }: { reset: boolean }) {
  const location = useLocation();
  const navigate = useNavigate();
  const route = reset ? '/reset-password' : '/forgot-password';
  const state: unknown = location.state;
  const [email, setEmail] = useState(() => typeof state === 'object' && state !== null && 'email' in state
    && typeof state.email === 'string' ? state.email.slice(0, 320) : '');
  const [visible, setVisible] = useState(false);
  const password = useRef<HTMLInputElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const errorPanel = useRef<HTMLParagraphElement>(null);
  const continueLink = useRef<HTMLAnchorElement>(null);
  function readLink() {
    const tokens = new URLSearchParams(location.hash.slice(1)).getAll('token');
    return { key: location.key, token: reset && tokens.length === 1 && /^[A-Za-z0-9_-]{43}$/.test(tokens[0] ?? '') ? tokens[0] : null };
  }
  const incoming = Boolean(location.search || (location.hash && location.hash !== '#main-content'));
  const [link, setLink] = useState(() => ({ ...readLink(), version: 0 }));
  const [attempt, setAttempt] = useState(-1);
  if (location.key !== link.key) setLink(incoming
    ? { ...readLink(), version: link.version + 1 } : { ...link, key: location.key });
  useEffect(() => {
    if (incoming) void navigate(route, { replace: true });
  }, [incoming, location.hash, location.search, navigate, route]);
  const action = useAuthAction(async () => {
    setAttempt(link.version);
    try {
      if (reset) {
        if (!link.token) throw new Error('Missing reset link.');
        const body: components['schemas']['ResetPasswordRequest'] = { token: link.token, password: password.current?.value ?? '' };
        await api.request<components['schemas']['RegistrationResult']>('/api/auth/reset-password', { method: 'POST', body });
        setLink(current => current.version === link.version ? { ...current, token: null } : current);
        await api.invalidateSession();
      } else {
        const body: components['schemas']['ForgotPasswordRequest'] = { email: email.trim() };
        await api.request<components['schemas']['RegistrationResult']>('/api/auth/forgot-password', { method: 'POST', body });
      }
      return link.version;
    } finally {
      if (password.current) password.current.value = '';
      setVisible(false);
    }
  });
  const accepted = action.isSuccess && action.data === link.version;
  const failed = action.isError && attempt === link.version;
  useEffect(() => {
    if (failed) (form.current?.querySelector<HTMLInputElement>('[aria-invalid="true"]') ?? errorPanel.current)?.focus();
    if (accepted) continueLink.current?.focus();
  }, [failed, action.error, accepted]);
  if (accepted) return <section className="ui-state auth-content" aria-label="Recovery request received">
    <p role="status">{reset
      ? 'Reset request received. An expired or already-used link cannot change your password. Try signing in with your new password, or request another link.'
      : 'If this email belongs to a password account, check your inbox for a reset link. Links last one hour.'}</p>
    <Link ref={continueLink} to="/login" state={{ email }}>Return to sign in</Link>
    {reset && <Link to="/forgot-password">Request a new reset link</Link>}
  </section>;
  if (reset && !link.token && !action.isPending) return <StatePanel state="error" title="Reset link unavailable"
    description="Open the link from your latest reset email. Links expire after one hour and can only be used once."
    action={<Link to="/forgot-password">Request a new reset link</Link>} />;
  return <form ref={form} className="ui-state auth-content" aria-label={reset ? 'Reset password' : 'Request password reset'}
    onSubmit={event => { event.preventDefault(); action.run(); }}>
    {reset ? <>
      <Field ref={password} label="New password" name="password" type={visible ? 'text' : 'password'} autoComplete="new-password"
        required minLength={15} maxLength={128} disabled={action.isPending}
        description="Use 15–128 characters. A valid reset signs out your existing sessions."
        error={failed ? fieldError(action.error, 'password') ?? '' : ''} />
      <Button tone="secondary" disabled={action.isPending} aria-pressed={visible} onClick={() => { setVisible(!visible); }}>
        {visible ? 'Hide password' : 'Show password'}
      </Button>
    </> : <Field label="Email address" name="email" type="email" autoComplete="username" required maxLength={320}
      value={email} onChange={event => { setEmail(event.target.value); }} disabled={action.isPending}
      error={failed ? fieldError(action.error, 'email') ?? '' : ''} />}
    {failed && <p ref={errorPanel} role="alert" tabIndex={-1}>{authError(action.error)}{reset && ' Your password was cleared.'}</p>}
    {action.wait > 0 && <p role="status">Try again in {action.wait} seconds.</p>}
    <Button type="submit" loading={action.isPending} disabled={action.wait > 0} loadingLabel="Sending request…">
      {reset ? 'Reset password' : 'Send reset link'}
    </Button>
    <Link to="/login" state={{ email }}>Return to sign in</Link>
  </form>;
}
