export const workspaceRoutes = [
  { path: '/profile', label: 'Profile', title: 'Your profile' },
  { path: '/searches', label: 'Searches', title: 'Saved searches' },
  { path: '/jobs', label: 'Jobs', title: 'Your job research' },
  { path: '/settings', label: 'Settings', title: 'Account settings' },
] as const;

export const authRoutes = [
  { path: '/login', title: 'Sign in' },
  { path: '/register', title: 'Create an account' },
  { path: '/verify-email', title: 'Verify your email' },
  { path: '/forgot-password', title: 'Forgot your password' },
  { path: '/reset-password', title: 'Reset your password' },
] as const;

export function safeReturnPath(value: string | null): string {
  const fallback = '/profile';
  if (!value || value.length > 2048 || !value.startsWith('/') || value.startsWith('//') || /[\\\p{Cc}]/u.test(value)) return fallback;
  try {
    const url = new URL(value, 'https://job-hunter.invalid');
    const path = decodeURIComponent(url.pathname);
    if (url.origin !== 'https://job-hunter.invalid' || /[\\%\p{Cc}]/u.test(path)
      || !workspaceRoutes.some(route => path === route.path || path.startsWith(`${route.path}/`))) return fallback;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}

export function sessionNotice(state: unknown) {
  if (typeof state !== 'object' || state === null || !('reason' in state)) return undefined;
  return state.reason === 'expired' || state.reason === 'signed_out' || state.reason === 'logout_failed'
    ? state.reason : undefined;
}
