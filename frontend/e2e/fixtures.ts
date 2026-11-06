import { test as base, expect } from '@playwright/test';
import { control, readState, type Scenario, type StackState } from './stack.ts';

export const test = base.extend<{ scenario: Scenario; stack: StackState; networkGuard: undefined }>({
  scenario: ['success', { option: true }],
  stack: [async ({ scenario }, use) => {
    await control('reset', '--scenario', scenario);
    await use(await readState());
  }, { auto: true }],
  networkGuard: [async ({ context, baseURL }, use) => {
    if (!baseURL) throw new Error('Missing local browser origin.');
    const errors: string[] = [];
    await context.route('**/*', async route => {
      if (new URL(route.request().url()).origin !== baseURL) {
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
