// Synthetic browser integration. Run after installing Playwright and Chromium:
// node tests/recall-observation.browser.mjs (from web/).
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chromium } from 'playwright';
const base = 'http://127.0.0.1:5189';
const server = spawn(process.execPath, ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '5189', '--strictPort'], { stdio: ['ignore', 'pipe', 'pipe'] });
let output = '';
server.stdout.on('data', chunk => { output += chunk; });
server.stderr.on('data', chunk => { output += chunk; });
let browser;
try {
  for (let attempt = 0; ; attempt++) {
    try { await fetch(base); break; } catch { if (attempt > 100 || server.exitCode != null) throw new Error(output); await new Promise(resolve => setTimeout(resolve, 100)); }
  }
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let calls = 0;
  const savedReviews = [];
  let navigations = 0;
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
  const makeRow = (id, status = 'completed') => ({ id, session_id: 'synthetic', created_at: '2026-01-01T10:00:00Z', payload: {
    observation_version: 1, observation_revision: 1, query: `Synthetic query ${id}`,
    request_kind: 'user_turn', request_status: status, recall_state: 'selected', memory_enabled: true,
    prepared_ids: ['scene:s', 'event:e'], prepared_items: [
      { id: 'scene:s', title: 'Synthetic memory', source_kind: 'scene', score: .509 },
      { id: 'event:e', title: 'Synthetic event', source_kind: 'event', score: .709 },
    ],
    injected_bucket_ids: status === 'completed' ? ['scene:s', 'event:e'] : [],
    recall_why_summary: { injected: status === 'completed' ? [
      { id: 'event:e', score: .709 },
      { id: 'scene:unselected', title: 'Unselected candidate', score: .99 },
    ] : [] },
  } });
  const rows = Array.from({ length: 40 }, (_, n) => makeRow(n + 1));
  rows[39] = makeRow(40, 'upstream_pending');
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== base) { await route.abort(); return; }
    if (!url.pathname.startsWith('/__serein/')) { await route.continue(); return; }
    let payload = { status: 'ok', items: [], has_more: false };
    const args = route.request().method() === 'POST' ? route.request().postDataJSON() || {} : {};
    if (url.pathname === '/__serein/gateway/injections') {
      calls++;
      const newer = args.afterId != null;
      let selected = rows.filter(row => newer ? row.id > args.afterId : !args.beforeId || row.id < args.beforeId);
      selected.sort((a, b) => newer ? a.id - b.id : b.id - a.id);
      const items = selected.slice(0, args.limit || 20);
      payload = { status: 'ok', items, has_more: selected.length > items.length,
        next_before_id: items.at(-1)?.id || null, next_after_id: newer ? items.at(-1)?.id || args.afterId : undefined,
        reviewed_items: rows.filter(row => args.reviewIds?.includes(row.id) && !items.some(item => item.id === row.id)),
      };
    } else if (url.pathname === '/__serein/personal' && route.request().method() === 'POST') {
      savedReviews.push(args);
      payload = { ...args, revision: 1 };
    } else if (url.pathname === '/__serein/gateway/semantic-routes') {
      payload = { routes: [{ name: 'general', label: '通用', action: 'recall', utterances: [] }], dataset_version: 1 };
    }
    await route.fulfill({ json: payload });
  });
  await page.goto(base + '/tests/recall-observation-preview.html');
  const cards = page.locator('[data-observation-id]');
  await cards.nth(19).waitFor();
  assert.equal(await cards.count(), 20);
  const current = page.locator('[data-observation-id="40"]');
  assert.match(await current.textContent(), /准备的记忆/);
  assert.match(await current.textContent(), /Synthetic memory/);
  assert.equal(await current.locator('.observation-memory-review').count(), 0, 'preparation is not reviewed as delivered');
  await current.evaluate(element => { window.firstObservationNode = element; });
  rows[39] = { ...makeRow(40), payload: { ...makeRow(40).payload, observation_revision: 3 } };
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.waitForFunction(() => document.querySelector('[data-observation-id="40"] .observation-outcome')?.textContent === '已注入');
  assert.equal(await current.evaluate(element => window.firstObservationNode === element), true);
  assert.match(await current.textContent(), /Synthetic memory/);
  assert.match(await current.textContent(), /已向聊天模型注入 2 条记忆/);
  assert.match(await current.textContent(), /真正送入模型/);
  assert.equal(await current.locator('.observation-memory-row').count(), 2);
  assert.deepEqual(await current.locator('.observation-memory-kind').allTextContents(), ['Scene', 'Event']);
  assert.deepEqual(await current.locator('.observation-memory-row em').allTextContents(), ['50.9%', '70.9%']);
  assert.doesNotMatch(await current.textContent(), /Unselected candidate/);
  const sceneReview = current.getByRole('group', { name: '记忆相关度：Synthetic memory', exact: true });
  await sceneReview.getByRole('button', { name: '核心相关', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[data-observation-id="40"] .observation-memory-review button.is-active')?.textContent === '核心相关');
  assert.ok(savedReviews.some(row => row.scope === 'recall_review' && row.key === '40' && row.value.candidateReviews?.['scene:s'] === 'core'), 'Gateway relevance saved through canonical personal API');
  assert.equal(await sceneReview.getByRole('button', { name: '重跑 shadow 校准' }).isDisabled(), true);

  const reading = page.locator('[data-observation-id="30"]');
  await reading.scrollIntoViewIfNeeded();
  await reading.getByRole('button', { name: '误召', exact: true }).click();
  await reading.locator('.observation-draft select').first().selectOption('general');
  const position = await reading.evaluate(element => element.getBoundingClientRect().top);
  rows.push(makeRow(41, 'upstream_pending'));
  const previousCalls = calls;
  await page.waitForFunction(() => document.querySelector('.observation-live-notice')?.textContent.includes('1 条新记录'), null, { timeout: 12_000 });
  assert.ok(calls > previousCalls, 'visible-page timer refreshed the list');
  assert.equal(await cards.count(), 20, 'new row remains buffered while reading history');
  assert.equal(await reading.locator('.observation-draft select').first().inputValue(), 'general');
  assert.ok(Math.abs(await reading.evaluate(element => element.getBoundingClientRect().top) - position) < 3, 'reading anchor retained');

  await page.locator('.observation-pagination').scrollIntoViewIfNeeded();
  await page.waitForFunction(() => document.querySelectorAll('[data-observation-id]').length === 40);
  assert.equal(await cards.count(), 40, 'scrolling appends a second page of 20');
  await page.getByRole('button', { name: '有 1 条新记录，点击查看' }).click();
  assert.equal(await cards.count(), 41);
  assert.equal(navigations, 1, 'no document reload');

  await page.evaluate(() => { window.testHidden = true; Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.testHidden }); document.dispatchEvent(new Event('visibilitychange')); });
  await page.waitForTimeout(300);
  const hiddenCalls = calls;
  await page.waitForTimeout(5300);
  assert.equal(calls, hiddenCalls, 'hidden browser tabs pause polling');
  await page.evaluate(() => { window.testHidden = false; document.dispatchEvent(new Event('visibilitychange')); });
  await page.waitForTimeout(500);
  assert.ok(calls > hiddenCalls, 'returning to the tab refreshes immediately');
  assert.deepEqual(errors, []);
  console.log('PASS browser: first 20, status update in place, buffered additions, draft + scroll preservation, next 20, no reload, hidden pause and resume');
} finally {
  if (browser) await browser.close();
  server.kill('SIGTERM');
}
