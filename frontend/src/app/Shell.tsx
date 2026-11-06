import { Component, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { useMutation, useQuery, type UseQueryResult } from '@tanstack/react-query';
import { Link, NavLink, Navigate, Outlet, Route, Routes, matchPath, useLocation, useNavigate } from 'react-router';
import { api, type Session } from '../shared/api';
import { Button, StatePanel } from '../shared/ui';
import { EmailAuth } from '../features/auth/EmailAuth';
import { GoogleCallback } from '../features/auth/GoogleSignIn';
import { Recovery } from '../features/auth/Recovery';
import { Verification, VerificationRequired } from '../features/auth/Verification';
import { Profile } from '../features/profile/Profile';
import { authRoutes, safeReturnPath, sessionNotice, workspaceRoutes } from './routes';

function FatalPage() {
  const main = useRef<HTMLElement>(null);
  useEffect(() => { document.title = 'Something went wrong · Job Hunter'; main.current?.focus(); }, []);
  return <main id="main-content" className="fatal-page" ref={main} tabIndex={-1}>
    <h1>Something went wrong</h1>
    <StatePanel state="error" title="This page could not be displayed"
      description="Reload the workspace to try again." action={<a href="/">Reload workspace</a>} />
  </main>;
}

export class AppErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <FatalPage /> : this.props.children; }
}

function AccountMenu({ email, pending, logout }: { email: string; pending: boolean; logout: () => void }) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  return <div className="account" onBlur={event => {
    if (!pending && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }} onKeyDown={event => {
    if (event.key === 'Escape') { setOpen(false); button.current?.focus(); }
  }}>
    <Button ref={button} tone="secondary" aria-expanded={open} aria-controls="account-panel"
      onClick={() => { setOpen(!open); }}>Account <span className="account-caret" aria-hidden="true" /></Button>
    {open && <div id="account-panel" className="account-panel">
      <p>Signed in as <strong>{email}</strong></p>
      <Link to="/settings" onClick={() => { setOpen(false); }}>Account settings</Link>
      <Button tone="secondary" loading={pending} loadingLabel="Signing out…" onClick={logout}>Sign out</Button>
    </div>}
  </div>;
}

function RequireSession({ session, signingOut }: { session: UseQueryResult<Session | null>; signingOut: RefObject<boolean> }) {
  const location = useLocation();
  const navigate = useNavigate();
  const hadSession = useRef(false);
  useEffect(() => {
    if (!session.isSuccess || signingOut.current) return;
    if (session.data) { hadSession.current = true; return; }
    const target = safeReturnPath(location.pathname + location.search + location.hash);
    void navigate(`/login?returnTo=${encodeURIComponent(target)}`, {
      replace: true, state: { reason: hadSession.current ? 'expired' : 'required' },
    });
  }, [session.isSuccess, session.data, signingOut, location, navigate]);
  if (session.isError) return <StatePanel state="error" title="We couldn’t check your session"
    description="Try again to open your workspace."
    action={<Button loading={session.isFetching} onClick={() => { void session.refetch(); }}>Try again</Button>} />;
  if (!session.data) return <StatePanel state="loading" title="Checking your session"
    description="Your workspace will open when the check is complete." />;
  return !session.data.email_verified && !matchPath('/settings', location.pathname) ? <VerificationRequired /> : <Outlet />;
}

function AuthPage({ session }: { session: Session | null }) {
  const location = useLocation();
  const notice = sessionNotice(location.state);
  if (session && matchPath('/register', location.pathname)) {
    return <Navigate replace to={safeReturnPath(new URLSearchParams(location.search).get('returnTo'))} />;
  }
  return <>
    {notice === 'expired' && <p role="status">Your session has expired. Sign in again to continue.</p>}
    {notice === 'signed_out' && <p role="status">You are signed out.</p>}
    {notice === 'logout_failed' && <StatePanel state="error" title="Sign out could not be confirmed"
      description="Your local workspace was cleared. Reload to check your session before trying again."
      action={<a href="/login">Check session again</a>} />}
    {matchPath('/verify-email', location.pathname) ? <Verification />
      : matchPath('/login', location.pathname) ? <GoogleCallback><EmailAuth key="login" mode="login" /></GoogleCallback>
      : matchPath('/register', location.pathname) ? <EmailAuth key="register" mode="register" />
      : <Recovery key={location.pathname} reset={Boolean(matchPath('/reset-password', location.pathname))} />}
  </>;
}

export function Shell() {
  const session = useQuery(api.currentSession);
  const location = useLocation();
  const navigate = useNavigate();
  const main = useRef<HTMLElement>(null);
  const previousPath = useRef(location.pathname);
  // Query notifications can precede router transitions; release the guard only on arrival.
  const signingOut = useRef(false);
  const user = session.isSuccess ? session.data : null;
  const title = location.pathname === '/' ? 'Your workspace'
    : [...workspaceRoutes, ...authRoutes].find(route => matchPath(route.path, location.pathname))?.title ?? 'Page not found';
  useEffect(() => {
    document.title = `${title} · Job Hunter`;
    if (location.pathname === '/login') signingOut.current = false;
    if (previousPath.current !== location.pathname) main.current?.focus();
    previousPath.current = location.pathname;
  }, [location.pathname, title]);
  const logout = useMutation({ mutationFn: () => api.logout(),
    onSuccess: () => { void navigate('/login', { replace: true, state: { reason: 'signed_out' } }); },
    onError: () => { void navigate('/login', { replace: true, state: { reason: 'logout_failed' } }); },
  });
  return <>
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="shell-header">
      <Link className="brand" to="/profile">Job Hunter<span>Your private workspace</span></Link>
      {user ? <AccountMenu key={location.pathname} email={user.email} pending={logout.isPending}
        logout={() => { signingOut.current = true; logout.mutate(); }} /> : <nav className="auth-navigation" aria-label="Account">
        <NavLink to="/login">Sign in</NavLink><NavLink to="/register">Create account</NavLink>
      </nav>}
    </header>
    <div className="shell-layout" data-authenticated={Boolean(user)}>
      {user && <nav className="workspace-navigation" aria-label="Workspace">
        {workspaceRoutes.map(route => <NavLink key={route.path} to={route.path}>{route.label}</NavLink>)}
      </nav>}
      <main id="main-content" ref={main} tabIndex={-1} className="shell-main">
        <p className="eyebrow">Your workspace</p><h1>{title}</h1>
        <Routes>
          <Route path="/" element={<Navigate to="/profile" replace />} />
          <Route element={<RequireSession session={session} signingOut={signingOut} />}>
            {workspaceRoutes.map(route => <Route key={route.path} path={route.path} element={
              route.path === '/profile' ? <Profile key={user?.id} ownerId={user?.id ?? ''} /> :
              <StatePanel state="empty" title={`${route.label} tools are not available yet`}
                description="This section will hold your workspace tools when they are available." />
            } />)}
          </Route>
          {authRoutes.map(route => <Route key={route.path} path={route.path}
            element={<AuthPage session={user} />} />)}
          <Route path="*" element={<StatePanel state="error" title="We couldn’t find that page"
            description="Check the address or return to your workspace."
            action={<Link to="/profile">Return to workspace</Link>} />} />
        </Routes>
      </main>
    </div>
  </>;
}
