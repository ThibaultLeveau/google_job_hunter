import { test, expect, mailLink, passwordForm, signIn, signOut } from './fixtures.ts';
import { control } from './stack.ts';

test('register, verify, sign in, navigate protected pages and sign out', async ({ page }) => {
  const user = { id: '', email: 'new-reader@example.org', password: 'Synthetic registration passphrase 2026!' };
  await page.goto('/register');
  await page.getByRole('textbox', { name: 'Email address', exact: true }).fill(user.email);
  await page.getByLabel(/^Password/).fill(user.password);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Registration received' })).toBeVisible();

  await page.goto(await mailLink(user.email, '/verify-email'));
  await expect(page).toHaveURL(/\/verify-email$/);
  await passwordForm(page, user.email, user.password);
  await page.getByRole('button', { name: 'Verify email', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Email verified', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Continue to your profile' }).click();
  await expect(page.getByRole('heading', { name: 'Your profile', exact: true })).toBeVisible();
  await signOut(page);

  await signIn(page, user);
  await page.getByRole('navigation', { name: 'Workspace' }).getByRole('link', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Account settings' })).toBeVisible();
  await signOut(page);
  await page.goto('/profile');
  await expect(page.getByRole('heading', { level: 1, name: 'Sign in', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Workspace' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Skills', exact: true })).toHaveCount(0);
});

test('recover a password and recover from an expired real session', async ({ page, stack }) => {
  const user = stack.users[0];
  if (!user) throw new Error('Missing seeded owner.');
  await signIn(page, user);
  await page.goto('/forgot-password');
  await page.getByRole('textbox', { name: 'Email address', exact: true }).fill(user.email);
  await page.getByRole('button', { name: 'Send reset link' }).click();
  await expect(page.getByRole('region', { name: 'Recovery request received' })).toBeVisible();
  await page.goto(await mailLink(user.email, '/reset-password'));
  await expect(page).toHaveURL(/\/reset-password$/);
  const password = 'Synthetic replacement passphrase 2026!';
  await page.getByLabel(/^New password/).fill(password);
  await page.getByRole('button', { name: 'Reset password', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Recovery request received' })).toBeVisible();
  await page.getByRole('link', { name: 'Return to sign in', exact: true }).click();
  await passwordForm(page, user.email, user.password);
  await expect(page.getByRole('alert')).toContainText('Your password was cleared.');
  await signIn(page, { ...user, password });

  await control('expire', '--owner', user.id);
  await page.getByRole('button', { name: 'Refresh documents', exact: true }).click();
  await expect(page.getByText('Your session has expired. Sign in again to continue.', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Skills', exact: true })).toHaveCount(0);
  await signIn(page, { ...user, password });
  await expect(page.getByText('No skills yet. Add a skill you want to use in your job search.', { exact: true })).toBeVisible();
});

test('synthetic Google identity creates a real backend session', async ({ context, page }) => {
  let authorizations = 0;
  await context.route('**/api/auth/google/start', async route => {
    // Playwright does not route subsequent requests in a server redirect chain.
    const response = await route.fetch({ maxRedirects: 0 });
    expect(response.status()).toBe(302);
    const authorization = response.headers().location;
    if (!authorization) throw new Error('Missing real OAuth authorization redirect.');
    const callback = await control('authorize', '--url', authorization);
    if (typeof callback !== 'string') throw new Error('Missing synthetic OAuth callback.');
    authorizations += 1;
    await route.fulfill({ response, headers: { ...response.headers(), location: callback } });
  });
  await page.goto('/login?returnTo=%2Fsettings');
  await page.getByRole('link', { name: 'Sign in with Google', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Account settings' })).toBeVisible();
  expect(authorizations).toBe(1);
  await expect(page).toHaveURL(/\/settings$/);
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  await expect(page.getByRole('banner').getByText('user@example.org', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  await page.reload();
  await expect(page.getByRole('heading', { level: 1, name: 'Account settings' })).toBeVisible();
  await signOut(page);
  await page.goto('/settings');
  await expect(page.getByRole('heading', { level: 1, name: 'Sign in', exact: true })).toBeVisible();
});
