import { useEffect, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router';
import type { components } from '../../../generated/api';
import { api, type Session } from '../../shared/api';
import { Button, Field } from '../../shared/ui';
import { safeReturnPath } from '../../app/routes';
import { GoogleSignIn } from './GoogleSignIn';
import { authError, fieldError, useAuthAction } from './useAuthAction';

export function EmailAuth({ mode }: { mode: 'login' | 'register' }) {
  const location = useLocation();
  const state: unknown = location.state;
  const [email, setEmail] = useState(() => typeof state === 'object' && state !== null && 'email' in state
    && typeof state.email === 'string' ? state.email.slice(0, 320) : '');
  const [visible, setVisible] = useState(false);
  const form = useRef<HTMLFormElement>(null);
  const password = useRef<HTMLInputElement>(null);
  const errorPanel = useRef<HTMLParagraphElement>(null);
  const continueLink = useRef<HTMLAnchorElement>(null);
  const target = safeReturnPath(new URLSearchParams(location.search).get('returnTo'));
  const registering = mode === 'register';
  const action = useAuthAction(async () => {
    const body: components['schemas']['RegistrationRequest'] = {
      email: email.trim(), password: password.current?.value ?? '',
    };
    try {
      if (registering) return await api.request<components['schemas']['RegistrationResult']>('/api/auth/register', { method: 'POST', body });
      const user = await api.request<Session>('/api/auth/login', { method: 'POST', body });
      api.acceptSession(user); return user;
    } finally {
      if (password.current) password.current.value = '';
      setVisible(false);
    }
  });
  useEffect(() => {
    if (action.isError) (form.current?.querySelector<HTMLInputElement>('[aria-invalid="true"]') ?? errorPanel.current)?.focus();
    if (registering && action.isSuccess) continueLink.current?.focus();
  }, [action.isError, action.error, action.isSuccess, registering]);
  if (registering && action.isSuccess) return <section className="ui-state auth-content" aria-label="Registration received">
    <p role="status">Request received. If a new account was created, a verification email is on its way. Sign in to check your account and complete verification.</p>
    <Link ref={continueLink} to={`/login?returnTo=${encodeURIComponent(target)}`} state={{ email }}>Sign in to continue</Link>
  </section>;
  return <form ref={form} className="ui-state auth-content" aria-label={registering ? 'Create account' : 'Sign in'}
    onSubmit={event => { event.preventDefault(); action.run(); }}>
    <Field label="Email address" name="email" type="email" autoComplete="username" required maxLength={320}
      value={email} onChange={event => { setEmail(event.target.value); }} disabled={action.isPending} error={fieldError(action.error, 'email') ?? ''} />
    <Field ref={password} label="Password" name="password" type={visible ? 'text' : 'password'} required
      autoComplete={registering ? 'new-password' : 'current-password'} minLength={registering ? 15 : 1} maxLength={128}
      {...(registering ? { description: 'Use 15–128 characters. Spaces and password managers are welcome.' } : {})}
      disabled={action.isPending} error={fieldError(action.error, 'password') ?? ''} />
    <Button tone="secondary" disabled={action.isPending} aria-pressed={visible} onClick={() => { setVisible(!visible); }}>
      {visible ? 'Hide password' : 'Show password'}
    </Button>
    {action.isError && <p ref={errorPanel} role="alert" tabIndex={-1}>{authError(action.error)} Your password was cleared.</p>}
    {action.wait > 0 && <p role="status">Try again in {action.wait} seconds.</p>}
    <Button type="submit" loading={action.isPending} disabled={action.wait > 0}
      loadingLabel={registering ? 'Creating account…' : 'Signing in…'}>{registering ? 'Create account' : 'Sign in'}</Button>
    <Link to={`/${registering ? 'login' : 'register'}?returnTo=${encodeURIComponent(target)}`} state={{ email }}>
      {registering ? 'Already have an account? Sign in' : 'Create an account'}
    </Link>
    {!registering && <Link to="/forgot-password" state={{ email }}>Forgot your password?</Link>}
    <GoogleSignIn />
  </form>;
}
