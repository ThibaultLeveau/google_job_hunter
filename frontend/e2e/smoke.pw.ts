import { test, expect } from './fixtures.ts';

test('real stack is healthy and public navigation works', async ({ page, request, stack }) => {
  expect(stack.users).toHaveLength(2);
  const ready = await request.get('/api/health/ready');
  expect(ready.status()).toBe(200);
  expect(await ready.json()).toEqual({ status: 'ok' });
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Sign in', exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: 'Account' }).getByRole('link', { name: 'Create account' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Create an account' })).toBeVisible();
  await page.getByRole('navigation', { name: 'Account' }).getByRole('link', { name: 'Sign in' }).click();
  await expect(page.getByRole('textbox', { name: 'Email address', exact: true })).toBeVisible();
  if (process.env.E2E_PROBE === 'outbound') {
    await page.evaluate(async () => { await fetch('https://unexpected.invalid/probe').catch(() => undefined); });
  }
  if (process.env.E2E_PROBE === 'failure') throw new Error('Intentional failure after readiness');
});
