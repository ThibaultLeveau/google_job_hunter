import { test as base, expect, type Page } from '@playwright/test';
import { createServer, request as httpRequest } from 'node:http';
import { connect } from 'node:net';
import type { Duplex } from 'node:stream';
import { control, readState, type Scenario, type StackState } from './stack.ts';

export const test = base.extend<{ scenario: Scenario; stack: StackState; networkGuard: undefined }>({
  scenario: ['success', { option: true }],
  stack: [async ({ scenario }, use) => {
    await control('reset', '--scenario', scenario);
    await use(await readState());
  }, { auto: true }],
  proxy: async ({ baseURL, stack }, use) => {
    const errors: string[] = [];
    const tunnels = new Set<Duplex>();
    const allowed = (url: URL) => [baseURL, stack.api_url].includes(url.origin) && !url.username && !url.password;
    const server = createServer((request, response) => {
      const target = new URL(request.url ?? '/', baseURL);
      if (!allowed(target)) {
        errors.push('Unexpected external request');
        response.writeHead(403).end();
        return;
      }
      const upstream = httpRequest(target, { method: request.method, headers: request.headers }, incoming => {
        response.writeHead(incoming.statusCode ?? 502, incoming.headers);
        incoming.pipe(response);
      });
      upstream.setTimeout(10000, () => { upstream.destroy(); });
      upstream.on('error', () => { if (response.headersSent) response.destroy(); else response.writeHead(502).end(); });
      response.on('close', () => { upstream.destroy(); });
      request.pipe(upstream);
    });
    server.on('connect', (request, socket, head) => {
      const target = new URL(`http://${request.url ?? ''}`);
      if (!allowed(target)) { errors.push('Unexpected external request'); socket.destroy(); return; }
      const upstream = connect({ host: '127.0.0.1', port: Number(target.port) });
      for (const stream of [socket, upstream]) {
        tunnels.add(stream);
        stream.once('close', () => { tunnels.delete(stream); });
      }
      upstream.setTimeout(10000, () => { upstream.destroy(); });
      upstream.on('error', () => { socket.destroy(); });
      socket.on('error', () => { upstream.destroy(); });
      socket.on('close', () => { upstream.destroy(); });
      upstream.once('connect', () => {
        socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
        if (head.length) upstream.write(head);
        socket.pipe(upstream).pipe(socket);
      });
    });
    server.on('upgrade', (_request, socket) => { errors.push('Unexpected WebSocket request'); socket.destroy(); });
    await new Promise<void>(done => { server.listen(0, '127.0.0.1', done); });
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('Missing local proxy address.');
    try {
      // Chromium otherwise bypasses proxies for loopback, including redirect chains.
      await use({ server: `http://127.0.0.1:${String(address.port)}`, bypass: '<-loopback>' });
    } finally {
      for (const stream of tunnels) stream.destroy();
      server.closeAllConnections();
      await new Promise<void>(done => { server.close(() => { done(); }); });
    }
    expect(errors, 'Unexpected browser proxy activity').toEqual([]);
  },
  networkGuard: [async ({ context, baseURL, stack }, use) => {
    if (!baseURL) throw new Error('Missing local browser origin.');
    const errors: string[] = [];
    await context.route('**/*', async route => {
      if (![baseURL, stack.api_url].includes(new URL(route.request().url()).origin)) {
        errors.push('Unexpected external request');
        await route.abort('blockedbyclient');
      } else await route.continue();
    });
    await context.routeWebSocket('**/*', async socket => {
      errors.push('Unexpected WebSocket request');
      await socket.close();
    });
    context.on('page', page => { page.on('pageerror', () => { errors.push('Unhandled browser error'); }); });
    await use(undefined);
    expect(errors, 'Unexpected browser activity').toEqual([]);
  }, { auto: true }],
});

export { expect };

export async function signIn(page: Page, user: StackState['users'][number]): Promise<void> {
  await page.goto('/login');
  await passwordForm(page, user.email, user.password);
  await expect(page.getByRole('button', { name: 'Account', exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: 'Workspace' }).getByRole('link', { name: 'Profile', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your profile', exact: true })).toBeVisible();
}

export async function passwordForm(page: Page, email: string, password: string): Promise<void> {
  await page.getByRole('textbox', { name: 'Email address', exact: true }).fill(email);
  await page.getByLabel(/^Password/).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
}

export async function signOut(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByText('You are signed out.', { exact: true })).toBeVisible();
}

export async function mailLink(recipient: string, path: string): Promise<string> {
  let link: string | undefined;
  await expect.poll(async () => {
    const messages = await control('mail') as { recipient: string; link: string }[];
    link = messages.find(message => message.recipient === recipient && new URL(message.link).pathname === path)?.link;
    return Boolean(link);
  }, { timeout: 30000, message: 'Synthetic mail was delivered by the worker' }).toBe(true);
  if (!link) throw new Error('Missing synthetic mail link.');
  return link;
}
