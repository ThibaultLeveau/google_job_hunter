import type { Page } from '@playwright/test';
import type { components } from '../generated/api';
import { test, expect, signIn } from './fixtures.ts';
import { control } from './stack.ts';

type Document = components['schemas']['Document'];
type DedupState = { documents: string[]; parsed: string[]; extractions: string[]; proposals: string[]; facts: string[]; stored_files: number };

async function uploadResume(page: Page, filename = 'synthetic-resume.txt'): Promise<void> {
  const fixture = await control('resume') as { text: string };
  await page.getByLabel('Choose a document, or drop one here').setInputFiles({ name: filename, mimeType: 'text/plain', buffer: Buffer.from(fixture.text) });
  await page.getByRole('button', { name: 'Upload document', exact: true }).click();
  await expect(page.getByText(/Extraction complete · 4 candidates/)).toBeVisible({ timeout: 30000 });
}

test('process a resume, review and edit facts, and reuse identical content across UI, API and database', async ({ page, stack }) => {
  const user = stack.users[0];
  if (!user) throw new Error('Missing seeded owner.');
  await signIn(page, user);
  await expect(page.getByText('No documents yet. Upload a document to extract profile candidates.', { exact: true })).toBeVisible();
  const uploaded = page.waitForResponse(response => response.url().endsWith('/api/v1/documents') && response.request().method() === 'POST');
  await uploadResume(page);
  const first = await uploaded;
  expect(first.status()).toBe(201);
  const document = await first.json() as Document;
  await expect(page.getByRole('region', { name: 'Skills', exact: true }).getByRole('heading', { name: 'Python', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Review candidates', exact: true }).click();
  const review = page.getByRole('region', { name: 'Review candidates from synthetic-resume.txt' });
  await expect(review.getByRole('heading', { name: 'Proposed experience: Développeur', exact: true })).toBeVisible();
  await expect(review.getByText('2020–2022', { exact: true })).toBeVisible();
  await expect(review.getByText('Uncertain', { exact: true })).toBeVisible();
  await review.getByRole('checkbox', { name: 'Select proposed skill: Python', exact: true }).check();
  await review.getByRole('checkbox', { name: 'Select proposed experience: Développeur', exact: true }).check();
  await review.getByRole('button', { name: 'Accept selected', exact: true }).click();
  await expect(review.getByText('2 created · 0 linked · 0 rejected · 0 conflict', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Skills', exact: true }).getByRole('heading', { name: 'Python', exact: true })).toBeVisible();
  const experience = page.getByRole('region', { name: 'Experience', exact: true });
  await expect(experience.getByRole('heading', { name: 'Développeur · Acme', exact: true })).toBeVisible();
  await expect(experience.getByText('Start date unknown — End date unknown · Ongoing status unknown', { exact: true })).toBeVisible();
  await experience.getByRole('button', { name: 'Edit experience', exact: true }).click();
  const editor = page.getByRole('dialog', { name: 'Edit experience', exact: true });
  await editor.getByRole('textbox', { name: 'Job title', exact: true }).fill('Développeur principal');
  await editor.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(editor).toHaveCount(0);
  await expect(experience.getByRole('heading', { name: 'Développeur principal · Acme', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Close review', exact: true }).click();

  await page.getByRole('textbox', { name: 'Skill name', exact: true }).fill(' python ');
  await page.getByRole('button', { name: 'Add skill', exact: true }).click();
  await expect(page.getByText('That skill is already in your profile. Your original wording was kept.', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Skills', exact: true }).getByRole('heading', { name: 'Python', exact: true })).toHaveCount(1);
  const before = await control('dedup', '--owner', user.id) as DedupState;
  expect(before.documents).toEqual([document.id]);
  expect(before.parsed).toHaveLength(1);
  expect(before.extractions).toHaveLength(1);
  expect(before.proposals).toHaveLength(4);
  expect(before.facts).toHaveLength(2);
  expect(before.stored_files).toBe(1);

  const duplicate = page.waitForResponse(response => response.url().endsWith('/api/v1/documents') && response.request().method() === 'POST');
  await uploadResume(page, 'same-content-renamed.txt');
  const response = await duplicate;
  expect(response.status()).toBe(200);
  expect(await response.json()).toMatchObject({ id: document.id, filename: 'synthetic-resume.txt', version: 1, status: 'ready' });
  await expect(page.getByText('This document already exists. Its original name and processing state are kept.', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'synthetic-resume.txt', exact: true })).toHaveCount(1);
  await expect(page.getByRole('heading', { name: 'same-content-renamed.txt', exact: true })).toHaveCount(0);
  await expect(experience.getByRole('heading', { name: 'Développeur principal · Acme', exact: true })).toBeVisible();
  const listed = await page.request.get('/api/v1/documents');
  expect(listed.status()).toBe(200);
  expect((await listed.json() as Document[]).map(item => item.id)).toEqual([document.id]);
  expect(await control('dedup', '--owner', user.id)).toEqual(before);

  await page.getByRole('button', { name: 'Delete synthetic-resume.txt', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Cancel deletion', exact: true })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Confirm deletion', exact: true })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByText('Deleted', { exact: true })).toBeVisible({ timeout: 30000 });
  const removed = await control('dedup', '--owner', user.id) as DedupState;
  expect(removed.documents).toEqual([]);
  expect(removed.parsed).toEqual([]);
  expect(removed.extractions).toEqual([]);
  expect(removed.proposals).toEqual([]);
  expect(removed.stored_files).toBe(0);
  expect(removed.facts).toEqual(before.facts);
  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await test.info().attach(`document-deleted-${String(viewport.width)}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
  }
  await page.getByRole('button', { name: 'Dismiss deleted document', exact: true }).click();
  const replacement = page.waitForResponse(result => result.url().endsWith('/api/v1/documents') && result.request().method() === 'POST');
  await uploadResume(page);
  const reuploaded = await replacement;
  expect(reuploaded.status()).toBe(201);
  const newDocument = await reuploaded.json() as Document;
  expect(newDocument.id).not.toBe(document.id);
  const after = await control('dedup', '--owner', user.id) as DedupState;
  expect(after.documents).toEqual([newDocument.id]);
  expect(after.stored_files).toBe(1);
  expect(after.facts).toEqual(before.facts);
});

test('stale review keeps selections and succeeds after explicit reload', async ({ page, context, stack }) => {
  const user = stack.users[0];
  if (!user) throw new Error('Missing seeded owner.');
  await signIn(page, user);
  await uploadResume(page);
  await page.getByRole('button', { name: 'Review candidates', exact: true }).click();
  const first = page.getByRole('region', { name: 'Review candidates from synthetic-resume.txt' });
  await first.getByRole('checkbox', { name: 'Select proposed skill: Python', exact: true }).check();
  const other = await context.newPage();
  await other.goto('/profile');
  await other.getByRole('button', { name: 'Review candidates', exact: true }).click();
  const stale = other.getByRole('region', { name: 'Review candidates from synthetic-resume.txt' });
  const selected = stale.getByRole('checkbox', { name: 'Select proposed experience: Développeur', exact: true });
  await selected.check();
  await first.getByRole('button', { name: 'Accept selected', exact: true }).click();
  await expect(first.getByText('1 created · 0 linked · 0 rejected · 0 conflict', { exact: true })).toBeVisible();
  await stale.getByRole('button', { name: 'Accept selected', exact: true }).click();
  await expect(stale.getByRole('alert')).toContainText('The profile revision changed');
  await expect(selected).toBeChecked();
  await expect(stale.getByRole('button', { name: 'Accept selected', exact: true })).toBeDisabled();
  await stale.getByRole('button', { name: 'Reload profile and candidates', exact: true }).click();
  await expect(stale.getByRole('alert')).toHaveCount(0);
  await expect(selected).toBeChecked();
  await stale.getByRole('button', { name: 'Accept selected', exact: true }).click();
  await expect(stale.getByText('1 created · 0 linked · 0 rejected · 0 conflict', { exact: true })).toBeVisible();
  await expect(other.getByRole('region', { name: 'Experience', exact: true }).getByRole('heading', { name: 'Développeur · Acme', exact: true })).toBeVisible();
  await other.close();
});

test('malformed TXT receives an honest unsupported outcome', async ({ page, stack }) => {
  const user = stack.users[0];
  if (!user) throw new Error('Missing seeded owner.');
  await signIn(page, user);
  await page.getByLabel('Choose a document, or drop one here').setInputFiles({ name: 'malformed.txt', mimeType: 'text/plain', buffer: Buffer.from([0, 255, 254]) });
  await page.getByRole('button', { name: 'Upload document', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveText('The file content is not supported. Choose a valid PDF, DOCX or UTF-8 TXT.');
  await expect(page.getByText('No documents yet. Upload a document to extract profile candidates.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Retry upload', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Review candidates', exact: true })).toHaveCount(0);
});
