import AxeBuilder from '@axe-core/playwright';
import type { Locator, Page, TestInfo } from '@playwright/test';
import type { components } from '../generated/api';
import { test, expect } from './fixtures.ts';
import { control } from './stack.ts';

type Jobs = components['schemas']['JobPage'];
type Detail = components['schemas']['JobDetail'];
const longTitle = 'Senior multilingual software engineer — ' + 'VeryLongUnbrokenTitle'.repeat(10);

async function reach(page: Page, target: Locator): Promise<void> {
  await expect(target).toBeVisible();
  await expect(target).toBeEnabled();
  for (let steps = 0; steps < 120; steps++) {
    if (await target.evaluate(element => element === document.activeElement)) {
      await expect(target).toBeFocused();
      expect(await target.evaluate(element => {
        const style = getComputedStyle(element);
        return style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) >= 2
          && [...element.getClientRects()].some(box => {
            const x = Math.max(0, Math.min(innerWidth - 1, box.left + box.width / 2));
            const y = Math.max(0, Math.min(innerHeight - 1, box.top + box.height / 2));
            return box.right > 0 && box.left < innerWidth && box.bottom > 0 && box.top < innerHeight
              && element.contains(document.elementFromPoint(x, y));
          });
      }), 'Keyboard focus is visible in the viewport').toBe(true);
      return;
    }
    await page.keyboard.press('Tab');
  }
  throw new Error('Control could not be reached by Tab.');
}

async function activate(page: Page, target: Locator, key = 'Enter'): Promise<void> {
  await reach(page, target);
  await page.keyboard.press(key);
}

async function write(page: Page, target: Locator, value: string): Promise<void> {
  await reach(page, target);
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText(value);
}

async function audit(page: Page, info: TestInfo, state: string, widths = [320, 768, 1440]): Promise<void> {
  for (const width of widths) for (const scale of [1, 2]) {
    await page.setViewportSize({ width, height: 900 });
    await page.evaluate(scale => { document.documentElement.style.fontSize = `${String(scale * 100)}%`; }, scale);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth),
      { message: `${state}: no horizontal overflow at ${String(width)}px / ${String(scale * 100)}% text` }).toBeLessThanOrEqual(1);
    for (const panel of await page.locator('dialog[open]').all()) {
      expect(await panel.evaluate(element => element.scrollWidth - element.clientWidth), `${state}: dialog reflows`).toBeLessThanOrEqual(1);
    }
    const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
    const name = `${state}-${String(width)}-${String(scale * 100)}`;
    await info.attach(`${name}-axe`, { body: JSON.stringify({ violations: result.violations, incomplete: result.incomplete }), contentType: 'application/json' });
    expect(result.violations.map(({ id, impact, nodes }) => ({ id, impact, targets: nodes.map(node => node.target) })), name).toEqual([]);
    // Native-dialog clipping can prevent axe sampling this paragraph; check its colors directly below.
    expect(result.incomplete.filter(rule => rule.id !== 'color-contrast'
      || rule.nodes.some(node => node.target.join() !== '#delete-help')), `${name}: unexpected manual-review items`).toEqual([]);
    if (width !== 768 && (scale === 1 || width === 320)) {
      const path = info.outputPath(`${name}.png`);
      await page.screenshot({ path, fullPage: await page.locator('dialog[open]').count() === 0 });
      await info.attach(name, { path, contentType: 'image/png' });
    }
  }
  await page.evaluate(() => { document.documentElement.style.fontSize = '100%'; });
  await page.setViewportSize({ width: widths.includes(1440) ? 1440 : 320, height: 900 });
}

test('core keyboard journeys, responsive text and axe checks', async ({ page, stack }, info) => {
  test.setTimeout(300000);
  const user = stack.users[0];
  if (!user) throw new Error('Missing synthetic owner.');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/login');
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  await audit(page, info, 'sign-in');
  await activate(page, page.getByRole('link', { name: 'Skip to main content', exact: true }));
  await expect(page.getByRole('main')).toBeFocused();
  await write(page, page.getByRole('textbox', { name: 'Email address', exact: true }), user.email);
  await write(page, page.getByLabel(/^Password/), 'Wrong synthetic password');
  let release = () => {};
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/auth/login', async route => { await gate; await route.continue(); }, { times: 1 });
  try {
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await activate(page, page.getByRole('button', { name: 'Sign in', exact: true }));
    const pending = page.getByRole('button', { name: 'Signing in…', exact: true });
    await expect(pending).toBeDisabled();
    await expect(pending.locator('.ui-spinner')).toHaveCSS('animation-name', 'ui-spin');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await expect(pending.locator('.ui-spinner')).toHaveCSS('animation-name', 'none');
    await expect(pending).toHaveCSS('transition-duration', '0s');
  } finally { release(); }
  await expect(page.getByRole('alert')).toBeFocused();
  await audit(page, info, 'sign-in-error');
  await write(page, page.getByLabel(/^Password/), user.password);
  await activate(page, page.getByRole('button', { name: 'Show password', exact: true }));
  await expect(page.getByLabel(/^Password/)).toHaveAttribute('type', 'text');
  await activate(page, page.getByRole('button', { name: 'Sign in', exact: true }));
  await expect(page.getByRole('button', { name: 'Account', exact: true })).toBeVisible();
  await activate(page, page.getByRole('navigation', { name: 'Workspace' }).getByRole('link', { name: 'Profile', exact: true }));
  await expect(page.getByRole('heading', { name: 'Your profile', exact: true })).toBeVisible();

  const fixture = await control('resume') as { text: string };
  // The browser file chooser is supplied a synthetic file; review and editing use only keys.
  await reach(page, page.getByLabel('Choose a document, or drop one here'));
  await page.getByLabel('Choose a document, or drop one here').setInputFiles({ name: 'accessibility-resume.txt', mimeType: 'text/plain', buffer: Buffer.from(fixture.text) });
  await activate(page, page.getByRole('button', { name: 'Upload document', exact: true }));
  await expect(page.getByText(/Extraction complete · 4 candidates/)).toBeVisible({ timeout: 30000 });
  await activate(page, page.getByRole('button', { name: 'Review candidates', exact: true }));
  const review = page.getByRole('region', { name: 'Review candidates from accessibility-resume.txt' });
  await expect(review.getByRole('checkbox', { name: 'Select proposed skill: Python', exact: true })).toBeEnabled();
  await expect(page.getByRole('navigation', { name: 'Document pages', exact: true })).toBeVisible();
  await audit(page, info, 'profile-review');
  await activate(page, review.getByRole('checkbox', { name: 'Select proposed skill: Python', exact: true }), 'Space');
  await activate(page, review.getByRole('checkbox', { name: 'Select proposed experience: Développeur', exact: true }), 'Space');
  await activate(page, review.getByRole('button', { name: 'Accept selected', exact: true }));
  await expect(review.getByText('2 created · 0 linked · 0 rejected · 0 conflict', { exact: true })).toBeVisible();
  await activate(page, page.getByRole('button', { name: 'Close review', exact: true }));
  const edit = page.getByRole('button', { name: 'Edit experience', exact: true });
  await activate(page, edit);
  const dialog = page.getByRole('dialog', { name: 'Edit experience', exact: true });
  await expect(dialog.getByRole('textbox', { name: 'Company', exact: true })).toBeFocused();
  await audit(page, info, 'profile-dialog');
  await reach(page, dialog.locator('summary'));
  await page.keyboard.press('Shift+Tab');
  await expect(dialog.getByRole('button', { name: 'Save changes', exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(edit).toBeFocused();
  await page.keyboard.press('Enter');
  await write(page, dialog.getByRole('textbox', { name: 'Job title', exact: true }), 'Développeur principal');
  await activate(page, dialog.getByRole('button', { name: 'Save changes', exact: true }));
  await expect(page.getByRole('heading', { name: 'Développeur principal · Acme', exact: true })).toBeVisible();
  await expect(edit).toBeFocused();

  await activate(page, page.getByRole('link', { name: 'Searches', exact: true }));
  await activate(page, page.getByRole('button', { name: 'New search', exact: true }));
  await write(page, page.getByRole('textbox', { name: 'Search name', exact: true }), 'Accessible Python search');
  await write(page, page.getByRole('textbox', { name: 'Group 1 term 1', exact: true }), 'Python');
  await activate(page, page.getByRole('button', { name: 'Add OR term to group 1', exact: true }));
  await expect(page.getByRole('textbox', { name: 'Group 1 term 2', exact: true })).toBeFocused();
  await page.keyboard.insertText('Go');
  await expect(page.getByRole('group', { name: 'Search logic', exact: true })).toBeVisible();
  await expect(page.getByRole('group', { name: 'Exact Google query', exact: true })).toBeVisible();
  await audit(page, info, 'search-editor');
  await activate(page, page.getByRole('button', { name: 'Remove term 2 from group 1', exact: true }));
  await expect(page.getByRole('textbox', { name: 'Group 1 term 1', exact: true })).toBeFocused();
  await activate(page, page.getByRole('button', { name: 'Save search', exact: true }));
  await expect(page.getByRole('heading', { name: 'Accessible Python search', exact: true })).toBeVisible();
  await reach(page, page.getByLabel('Saved search on this page', { exact: true }));
  await page.keyboard.press('Home');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await activate(page, page.getByRole('button', { name: 'Run search', exact: true }));
  await expect.poll(async () => {
    const state = await control('jobs', '--owner', user.id) as { matches: unknown[] };
    return state.matches.length;
  }, { timeout: 60000 }).toBe(1);
  await activate(page, page.getByRole('link', { name: 'Jobs', exact: true }));
  await expect(page.getByRole('link', { name: 'Python engineer', exact: true })).toBeVisible();
  await audit(page, info, 'jobs');
  await page.setViewportSize({ width: 320, height: 900 });
  const filters = page.getByRole('button', { name: 'Filters and sorting', exact: true });
  await activate(page, filters);
  const drawer = page.getByRole('dialog', { name: 'Filter and sort jobs', exact: true });
  await expect(drawer.getByRole('textbox', { name: 'Keyword', exact: true })).toBeFocused();
  await audit(page, info, 'filter-drawer', [320]);
  await page.keyboard.press('Shift+Tab');
  await expect(drawer.getByRole('button', { name: 'Close filters', exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(filters).toBeFocused();
  await activate(page, page.getByRole('link', { name: 'Python engineer', exact: true }));
  await expect(page.getByRole('heading', { name: 'Job details', exact: true })).toBeVisible();
  const detailPath = new URL(page.url()).pathname;
  await activate(page, page.getByText('Full checklist and evidence (1 items)', { exact: true }));
  await activate(page, page.getByText('Source evidence (1 excerpts)', { exact: true }));
  await expect(page.getByRole('blockquote').first()).toHaveText('Python required');
  await audit(page, info, 'job-detail');

  // Rendering stress uses real responses with explicitly synthetic long/unknown display values.
  await page.route('**/api/v1/jobs?*', async route => {
    const response = await route.fetch(), data = await response.json() as Jobs;
    await route.fulfill({ response, json: { ...data, items: data.items.map(job => ({ ...job,
      title: { status: 'known', values: [longTitle] }, company: { status: 'unknown', values: [] },
      location: { status: 'unknown', values: [] }, region: { status: 'unknown', values: [] }, remote: 'unknown',
      salary: { status: 'unknown', values: [] }, match: { ...job.match, score: null, assessed_percent: null },
    })) } });
  });
  await page.goto('/jobs');
  await expect(page.getByRole('link', { name: longTitle, exact: true })).toBeVisible();
  await expect(page.getByText('Company unknown', { exact: true })).toBeVisible();
  await expect(page.getByText('Salary unknown', { exact: true })).toBeVisible();
  await audit(page, info, 'jobs-long-unknown');
  await page.route(`**/api/v1${detailPath}`, async route => {
    const response = await route.fetch(), data = await response.json() as Detail;
    await route.fulfill({ response, json: { ...data, metadata: null, match: null, match_context: null } });
  });
  await activate(page, page.getByRole('link', { name: longTitle, exact: true }));
  await expect(page.getByRole('heading', { name: 'Job title unknown', exact: true })).toBeVisible();
  await audit(page, info, 'detail-unknown');
  await activate(page, page.getByRole('button', { name: 'Account', exact: true }));
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Account', exact: true })).toBeFocused();
  await activate(page, page.getByRole('button', { name: 'Account', exact: true }));
  await activate(page, page.getByRole('link', { name: 'Account settings', exact: true }));
  await expect(page.getByRole('heading', { name: 'Sign-in and verification', exact: true })).toBeVisible();
  await audit(page, info, 'settings');
  const deletion = page.getByRole('button', { name: 'Delete my account', exact: true });
  await activate(page, deletion);
  const confirmation = page.getByRole('dialog', { name: 'Permanently delete your account?', exact: true });
  await expect(confirmation.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await audit(page, info, 'delete-dialog');
  const colors = await confirmation.evaluate(element => [
    getComputedStyle(element.querySelector('#delete-help') ?? element).color,
    getComputedStyle(element).backgroundColor,
  ]);
  const luminance = colors.map(color => {
    expect(color).toMatch(/^rgb\(/);
    const channels = (color.match(/[\d.]+/g) ?? []).map(Number);
    expect(channels).toHaveLength(3);
    return channels.reduce((total, value, index) => {
      const channel = value / 255;
      return total + (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4) * ([0.2126, 0.7152, 0.0722][index] ?? 0);
    }, 0);
  });
  expect((Math.max(...luminance) + 0.05) / (Math.min(...luminance) + 0.05)).toBeGreaterThanOrEqual(4.5);
  await page.keyboard.press('Tab');
  await expect(confirmation.getByLabel('Type DELETE to confirm', { exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(deletion).toBeFocused();
  await activate(page, page.getByRole('button', { name: 'Account', exact: true }));
  await activate(page, page.getByRole('button', { name: 'Sign out', exact: true }));
  await expect(page.getByText('You are signed out.', { exact: true })).toBeVisible();
});

test('registration, recovery and verification states reflow accessibly', async ({ page }, info) => {
  test.setTimeout(180000);
  for (const [path, heading] of [
    ['/register', 'Create an account'], ['/forgot-password', 'Forgot your password'],
    ['/reset-password', 'Reset your password'], ['/verify-email', 'Verify your email'],
  ]) {
    if (!path || !heading) throw new Error('Missing authentication audit case.');
    await page.goto(path);
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    await expect(page.getByText('Checking your account', { exact: true })).toHaveCount(0);
    await audit(page, info, path.slice(1));
  }
});
