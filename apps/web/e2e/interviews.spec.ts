import { expect, test } from '@playwright/test';
import { DEFAULT_SETUP, type InterviewSession } from '../lib/interviews/types';
test('prepare, reload, practice, honest failed grade and repeat context', async ({ page }) => {
  let saved: InterviewSession | null = null;
  let lastCreate: Record<string, unknown> = {};
  await page.route('**/api/imx/interviews**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/context')) return route.fulfill({ json: { resumeText: 'Fixture candidate managed acquisition.', jobs: [] } });
    if (path === '/api/imx/interviews') {
      if (route.request().method() === 'POST') { lastCreate = route.request().postDataJSON(); saved = { ...DEFAULT_SETUP, ...(lastCreate.previousSessionId ? saved : {}), ...route.request().postDataJSON(), id: 'fixture-practice', status: 'draft', createdAt: new Date().toISOString(), documents: [], turns: [], sourceIngestion: { status: 'processing' }, contextWarnings: ['No prior-round material supplied.'] }; return route.fulfill({ json: saved }); }
      return route.fulfill({ json: { readiness: { fixture: 'UI test only; no live provider verified' }, sessions: saved ? [saved] : [] } });
    }
    if (!saved) return route.fulfill({ status: 404, json: { error: { message: 'Missing fixture' } } });
    if (route.request().method() === 'GET' && saved.sourceIngestion?.status === 'processing') saved = { ...saved, sourceIngestion: {status: 'ready'}, jobDescription: 'Imported job context: own acquisition and report evidence.', documents: [{id: 'web-job', kind: 'job', name: 'Imported job page', sourceUrl: saved.jobUrl, status: 'ready', chunkCount: 2}] };
    if (path.endsWith('/start')) saved = { ...saved, status: 'active', deadlineAt: new Date(Date.now() + 30 * 60_000).toISOString(), currentQuestion: 'What was your measurable impact?' };
    if (path.endsWith('/turns')) { const payload = route.request().postDataJSON(); saved = { ...saved, turns: [{ id: 't1', requestId: payload.requestId, question: saved.currentQuestion!, answer: payload.answer, scoreStatus: 'failed', error: 'Provider unavailable', score: null }] }; }
    if (path.endsWith('/finish')) saved = { ...saved, status: 'completed', report: { overallScore: null, gradedAnswers: 0, totalAnswers: 1, nextDrills: [], coverage: 'Incomplete practice. No provider judgment available.' } };
    return route.fulfill({ json: saved });
  });
  await page.goto('/interviews');
  await expect(page.getByRole('heading', { name: /Make your next answer/ })).toBeVisible();
  await page.getByLabel('Company', { exact: true }).fill('Fixture Company');
  await page.getByLabel('Role title').fill('Marketing lead');
  await page.getByLabel('Company website URL').fill('https://fixture.example.com');
  await page.getByLabel('Job application URL').fill('https://fixture.example.com/jobs/lead');
  await expect(page.getByLabel('Resume / candidate evidence')).toHaveValue('Fixture candidate managed acquisition.');
  await page.getByRole('button', { name: 'Save practice setup' }).click();
  await expect(page.getByText('No prior-round material supplied.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start 30-minute practice' })).toBeDisabled();
  await expect(page.getByText(/Reading the company website/)).toBeVisible();
  expect(lastCreate.jobDescription).toBe('');
  await expect(page.getByText(/Website context is ready/)).toBeVisible();
  await expect(page.getByRole('link', {name: 'Imported job page ↗'})).toHaveAttribute('href', 'https://fixture.example.com/jobs/lead');
  await page.reload();
  await page.getByRole('button', { name: /Fixture Company/ }).click();
  await page.getByRole('button', { name: 'Edit setup', exact: true }).click();
  await page.getByLabel('Duration (minutes)').fill('45');
  await page.getByLabel('Interviewer role').fill('executive');
  await page.getByRole('button', { name: 'Save revised setup' }).click();
  expect(lastCreate.previousSessionId).toBe('fixture-practice');
  expect(lastCreate.resumeText).toBeUndefined();
  await page.getByRole('button', { name: 'Start 45-minute practice' }).click();
  await expect(page.getByRole('button', { name: 'Enable microphone & connect' })).toBeVisible();
  await expect(page.getByText('Disconnected · Microphone off')).toBeVisible();
  await page.getByText('Use text fallback').click();
  await page.getByLabel('Your answer').fill('My fixture answer with no invented score.');
  await page.getByRole('button', { name: 'Send answer' }).click();
  await expect(page.getByText('Judgment failed · no score')).toBeVisible();
  await page.getByRole('button', { name: 'End practice', exact: true }).click();
  await expect(page.getByText('0 of 1 answers graded')).toBeVisible();
  await page.getByRole('button', { name: /Practice again with this context/ }).click();
  await expect(page.getByLabel('Company', { exact: true })).toHaveValue('Fixture Company');
  await page.getByRole('button', { name: 'Save practice setup' }).click();
  await expect(page.getByRole('button', { name: 'Start 45-minute practice' })).toBeVisible();
  expect(lastCreate.previousSessionId).toBe('fixture-practice');
  expect(lastCreate.resumeText).toBeUndefined(); // Preserve uploaded resume documents on repeat.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});


test('failed website import blocks practice and can be retried without pasting a job description', async ({ page }) => {
  let saved: InterviewSession | null = null;
  let retried = false;
  await page.route('**/api/imx/interviews**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/context')) return route.fulfill({json: {resumeText: '', jobs: []}});
    if (path === '/api/imx/interviews') {
      if (route.request().method() === 'POST') {
        saved = {...DEFAULT_SETUP, ...route.request().postDataJSON(), id: 'failed-source', status: 'draft', createdAt: new Date().toISOString(), documents: [], turns: [], contextWarnings: [], sourceIngestion: {status: 'failed', error: 'Website reader unavailable.'}};
        return route.fulfill({json: saved});
      }
      return route.fulfill({json: {readiness: {}, sessions: saved ? [saved] : []}});
    }
    if (!saved) return route.fulfill({status: 404, json: {error: {message: 'Missing fixture'}}});
    if (path.endsWith('/ingest')) { retried = true; saved = {...saved, sourceIngestion: {status: 'processing'}}; }
    else if (retried && route.request().method() === 'GET') saved = {...saved, sourceIngestion: {status: 'ready'}, jobDescription: 'Job context imported after retry.'};
    return route.fulfill({json: saved});
  });
  await page.goto('/interviews');
  await page.getByLabel('Company', {exact: true}).fill('Fixture');
  await page.getByLabel('Role title').fill('Lead');
  await expect(page.getByLabel('Company website URL')).toHaveAttribute('required', '');
  await expect(page.getByLabel('Job application URL')).toHaveAttribute('required', '');
  await page.getByLabel('Company website URL').fill('https://fixture.example.com');
  await page.getByLabel('Job application URL').fill('https://fixture.example.com/jobs/lead');
  await page.getByRole('button', {name: 'Save practice setup'}).click();
  await expect(page.getByRole('main').getByRole('alert')).toContainText('Website reader unavailable.');
  await expect(page.getByRole('button', {name: 'Start 30-minute practice'})).toBeDisabled();
  await page.getByRole('button', {name: 'Retry website import'}).click();
  await expect(page.getByText(/Reading the company website/)).toBeVisible();
  await expect(page.getByRole('button', {name: 'Start 30-minute practice'})).toBeEnabled();
  expect(retried).toBe(true);
});
