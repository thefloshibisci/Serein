// Real React interactions; every fetch is intercepted with synthetic records.
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://synthetic.invalid', pretendToBeVisual: true });
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'CustomEvent', 'Node']) {
  Object.defineProperty(globalThis, key, { value: dom.window[key], configurable: true });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
HTMLElement.prototype.scrollTo = () => {};
HTMLElement.prototype.scrollIntoView = () => {};
let hidden = false;
Object.defineProperty(document, 'hidden', { get: () => hidden, configurable: true });
Object.defineProperty(document, 'visibilityState', { get: () => hidden ? 'hidden' : 'visible', configurable: true });

// Only control the production 15-second refresh timer; React and search delays
// retain real scheduling. No wall-clock wait is needed to exercise polling.
const setTimer = window.setTimeout.bind(window), clearTimer = window.clearTimeout.bind(window);
const polls = new Map(), deadlines = new Map();
let nextTimer = 1_000_000;
window.setTimeout = (callback, delay, ...args) => {
  if (delay !== 15_000 && delay !== 20_000) return setTimer(callback, delay, ...args);
  const id = nextTimer++; (delay === 15_000 ? polls : deadlines).set(id, () => callback(...args)); return id;
};
window.clearTimeout = id => polls.delete(id) || deadlines.delete(id) || clearTimer(id);
function poll() {
  assert.equal(polls.size, 1, 'Exactly one non-overlapping refresh is scheduled');
  const [id, callback] = polls.entries().next().value;
  polls.delete(id); callback();
}
const gate = () => {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
};
const event = (id, title, day = '10', body = `Synthetic body ${id}`) => ({
  item_id: id, item_type: 'event', title, body, status: 'active', recallable: true,
  local_date: `2026-11-${day}`, local_start_time: '10:00', local_end_date: `2026-11-${day}`, local_end_time: '11:00',
  source_count: 1,
});
const old = event('event_old', 'Old cached Event', '08');
let records = [old, event('event_new', 'New background Event', '09')];
let fail = false, listGate = null, respectAbort = false, queryGate = null, failQuery = false, badList = '';
const calls = [];
const reply = (value, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => structuredClone(value) });
window.localStorage.setItem('serein.personal.imported.v1', '1');
window.localStorage.setItem('serein.memory.fact-events.v2', JSON.stringify({ cachedAt: Date.now(), fact: [], event: [old] }));
globalThis.fetch = window.fetch = async (input, options = {}) => {
  const path = String(input), body = options.body ? JSON.parse(options.body) : {};
  calls.push({ path, body, signal: options.signal });
  if (path === '/__serein/settings') return reply({ features: { facts: true }, identity: { user_name: 'User', ai_name: 'Fixture' } });
  if (path.startsWith('/__serein/personal')) return reply({ items: [], has_more: false });
  if (path === '/__serein/live/memory-scenes') return reply({ status: 'ok', scenes: [], edges: [] });
  if (path === '/__serein/live/fact-events') {
    assert.equal(options.cache, 'no-store');
    let items = body.type === 'event' ? structuredClone(records) : [];
    if (body.status !== 'all') items = items.filter(item => item.status === body.status);
    if (body.query) {
      const terms = body.query.trim().toLocaleLowerCase('zh-CN').split(/\s+/u);
      items = items.filter(item => terms.every(term => `${item.title} ${item.body}`.toLocaleLowerCase('zh-CN').includes(term)));
      if (body.includeSources) items = items.map(item => ({ ...item, source_refs: [{ source_system: 'synthetic', session_id: 'fixture', message_id: 1, role: 'user', created_at: '2026-11-08T01:00:00Z', content: 'Kept source text' }] }));
      if (queryGate) await queryGate.promise;
      if (failQuery) return reply({ message: 'Synthetic search outage' }, 503);
      return reply({ items, count: items.length });
    }
    const frozenFailure = fail;
    if (body.status === 'active' && badList === 'malformed') return reply({ count: 0 });
    if (body.status === 'active' && badList === 'empty-page') return reply({ items: [], count: 2 });
    if (body.status === 'active' && badList === 'duplicate-page') return reply({ items: [items[0]], count: 2 });
    if (listGate) {
      // Most cases ignore abort to exercise stale-response guards. The timeout
      // case behaves like browser fetch and rejects as soon as it is aborted.
      if (respectAbort) await new Promise((resolve, reject) => {
        const abort = () => reject(new DOMException('Aborted', 'AbortError'));
        options.signal.addEventListener('abort', abort, { once: true });
        listGate.promise.then(() => { options.signal.removeEventListener('abort', abort); resolve(); });
      });
      else await listGate.promise;
    }
    return frozenFailure ? reply({ message: 'Synthetic temporary outage' }, 503) : reply({ items, count: items.length });
  }
  if (path === '/__serein/memory/set-fact-event-status') {
    const item = records.find(item => item.item_id === body.itemId);
    assert(item); item.status = body.status;
    return reply({ item });
  }
  throw new Error(`Unexpected synthetic request: ${path}`);
};
const listCalls = () => calls.filter(call => call.path === '/__serein/live/fact-events' && !call.body.query);
const directory = await mkdtemp(join(tmpdir(), 'serein-memory-refresh-dom-'));
let root;
try {
  const output = await build({ stdin: { contents: `import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {MemoryPage} from './src/pages/MemoryPage.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<MemoryPage/>);return root;}`, resolveDir: process.cwd(), loader: 'jsx' }, jsx: 'automatic', bundle: true, platform: 'node', format: 'esm', write: false, loader: { '.css': 'empty' }, define: { 'process.env.NODE_ENV': '"development"', 'import.meta.env.BASE_URL': '"/"' } });
  const path = join(directory, 'bundle.mjs'); await writeFile(path, output.outputFiles[0].text);
  const { act, mount } = await import(pathToFileURL(path));
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function settle(action, ms = 30) { await act(async () => { action?.(); await wait(ms); }); }
  const button = (text, within = document) => [...within.querySelectorAll('button')].find(node => node.textContent.trim() === text || node.getAttribute('aria-label') === text);
  async function click(node) { assert(node, 'Button exists'); assert(!node.disabled, 'Button enabled'); await settle(() => node.click()); }
  async function value(node, text, ms = 30) {
    assert(node, 'Input exists'); await settle(() => {
      Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), 'value').set.call(node, text);
      node.dispatchEvent(new Event('input', { bubbles: true }));
    }, ms);
  }
  const titles = () => [...document.querySelectorAll('.fact-event-timeline .scene-entry strong')].map(node => node.textContent);
  const entry = id => [...document.querySelectorAll('.fact-event-timeline .scene-entry')].find(node => node.querySelector('strong').textContent === id);
  const refreshButton = () => button('刷新事件列表');
  await settle(() => { root = mount(); });
  assert.equal(listCalls().length, 0, 'Scene view does not poll hidden Event/Fact lists');
  listGate = gate();
  await click(button('事件', document.querySelector('.memory-type-switch')));
  assert.deepEqual(titles(), ['Old cached Event'], 'Even a fresh cache remains readable during immediate validation');
  assert.equal(listCalls().length, 2, 'Entry validates only the selected type, both lifecycle lists');
  assert.equal(refreshButton().disabled, true);
  await settle(() => { window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange')); });
  assert.equal(listCalls().length, 2, 'Focus and visibility events share the in-flight read');
  await settle(() => { listGate.release(); listGate = null; });
  assert.deepEqual(titles(), ['New background Event', 'Old cached Event']);
  assert.equal(JSON.parse(window.localStorage.getItem('serein.memory.fact-events.v2')).event.length, 2);

  await click(entry('Old cached Event').querySelector('button'));
  assert.match(document.querySelector('.fact-event-detail').textContent, /Kept source text/);
  await click(button('编辑正文', document.querySelector('.fact-event-detail')));
  await value(document.querySelector('.fact-event-detail textarea'), 'An unsaved draft');
  records = [event('event_latest', 'New while reading'), { ...old, title: 'Updated server title' }, records[1]];
  await settle(poll);
  assert.deepEqual(titles(), ['New while reading', 'New background Event', 'Updated server title']);
  assert.equal(document.querySelector('.fact-event-detail textarea').value, 'An unsaved draft', 'Background updates retain a draft and its detail reader');
  assert.match(document.querySelector('.fact-event-detail').textContent, /Kept source text/, 'Read source expansion stays open');
  await click(button('取消', document.querySelector('.fact-event-detail')));
  assert.equal(document.querySelector('.fact-event-detail h2').textContent, 'Old cached Event', 'Already-open detail stays on its explicitly read snapshot');
  await click(button('关闭详情'));

  await click(button('批量整理'));
  await click(entry('New background Event').querySelector('button'));
  records.push(event('event_after_selection', 'New during selection', '11'));
  await settle(poll);
  assert.match(document.querySelector('.memory-batch-toolbar').textContent, /已选 1 条/, 'Polling retains selected IDs');
  assert.equal(entry('New background Event').querySelector('button').getAttribute('aria-pressed'), 'true');
  await click(button('退出批量'));

  hidden = true;
  const beforeHidden = listCalls().length;
  await settle(() => { document.dispatchEvent(new Event('visibilitychange')); window.dispatchEvent(new Event('focus')); });
  assert.equal(polls.size, 0, 'Hidden tabs have no polling timer');
  assert.equal(listCalls().length, beforeHidden);
  hidden = false; fail = true;
  await settle(() => document.dispatchEvent(new Event('visibilitychange')));
  assert(titles().includes('New during selection'), 'Transient failures keep the last readable list');
  assert.match(document.querySelector('.memory-action-message.is-error').textContent, /暂未更新.*上次读取.*Synthetic temporary outage/);
  fail = false;
  records.push(event('event_recovered', 'Read after reconnect', '12'));
  await click(refreshButton());
  assert.equal(document.querySelector('.memory-action-message.is-error'), null, 'Success clears the stale-data warning');
  assert.equal(titles()[0], 'Read after reconnect');

  listGate = gate(); respectAbort = true;
  await click(refreshButton());
  assert.equal(deadlines.size, 2);
  await settle(() => { for (const [id, callback] of [...deadlines]) { deadlines.delete(id); callback(); } });
  assert.match(document.querySelector('.memory-action-message.is-error').textContent, /读取超时/);
  assert.equal(refreshButton().disabled, false, 'A stalled network read releases the refresh lock');
  listGate.release(); listGate = null; respectAbort = false;
  await settle(poll);
  assert.equal(document.querySelector('.memory-action-message.is-error'), null, 'The next automatic pass recovers from a stalled read');
  for (badList of ['malformed', 'empty-page', 'duplicate-page']) {
    const previous = titles();
    await click(refreshButton());
    assert.deepEqual(titles(), previous, `${badList} cannot replace a verified list with a partial or empty snapshot`);
    assert.match(document.querySelector('.memory-action-message.is-error').textContent, /暂未更新/);
  }
  badList = '';
  await click(refreshButton());
  assert.equal(document.querySelector('.memory-action-message.is-error'), null);

  records.push(event('event_terms', '章章送来的图', '13', '温霁礼收到了。'));
  records.push({ ...event('event_archived', '章章旧图', '14', '温霁礼旧材料。'), status: 'archived' });
  await value(document.querySelector('input[type="search"]'), '章章   温霁礼', 220);
  await settle(undefined, 60);
  assert.deepEqual(titles(), ['章章送来的图'], 'Dispersed title/body terms and extra spaces use server search matches while respecting active status');
  records.push(event('event_terms_new', '后来章章的信', '15', '温霁礼又回信了。'));
  await settle(poll);
  await settle(undefined, 220);
  assert.deepEqual(titles(), ['后来章章的信', '章章送来的图'], 'A standing query refreshes after new background material arrives');
  queryGate = gate();
  await settle(poll); await settle(undefined, 220);
  assert.deepEqual(titles(), ['后来章章的信', '章章送来的图'], 'Refreshing a standing query keeps readable results on screen');
  const slowQuery = calls.filter(call => call.body.query && !call.body.includeSources).at(-1);
  await settle(poll); await settle(undefined, 220);
  assert.equal(slowQuery.signal.aborted, false, 'A poll does not cancel and starve a slower current search');
  assert.equal(calls.filter(call => call.body.query && !call.body.includeSources).at(-1), slowQuery);
  failQuery = true;
  await settle(() => { queryGate.release(); queryGate = null; });
  assert.deepEqual(titles(), ['后来章章的信', '章章送来的图'], 'A failed query refresh retains its prior result');
  assert.match(document.querySelector('.memory-action-message.is-error').textContent, /Synthetic search outage/);
  failQuery = false;
  await settle(poll); await settle(undefined, 220);
  assert.equal(document.querySelector('.memory-action-message.is-error'), null);
  await click(button('清空搜索'));

  listGate = gate();
  await settle(() => window.dispatchEvent(new Event('focus')));
  const staleCalls = listCalls().slice(-2);
  const cachedBeforeExit = window.localStorage.getItem('serein.memory.fact-events.v2');
  await click(button('Scene', document.querySelector('.memory-type-switch')));
  assert(staleCalls.every(call => call.signal.aborted), 'Leaving Event cancels both list requests');
  await settle(() => { listGate.release(); listGate = null; });
  assert.equal(window.localStorage.getItem('serein.memory.fact-events.v2'), cachedBeforeExit, 'A late response cannot overwrite the cache after leaving');
  assert.equal(polls.size, 0);
  await click(button('事件', document.querySelector('.memory-type-switch')));
  assert(titles().includes('后来章章的信'), 'Re-entry validates new records regardless of cache age');

  // A read started before an archive receipt must never resurrect that item.
  await click(entry('New background Event').querySelector('button'));
  listGate = gate();
  await settle(() => window.dispatchEvent(new Event('focus')));
  await click(button('归档', document.querySelector('.fact-event-detail')));
  assert(!titles().includes('New background Event'));
  await settle(() => { listGate.release(); listGate = null; });
  assert(!titles().includes('New background Event'), 'Old in-flight list cannot undo a confirmed archive');

  listGate = gate();
  await settle(poll);
  const pendingCalls = listCalls().slice(-2);
  const cachedBeforeUnmount = window.localStorage.getItem('serein.memory.fact-events.v2');
  await settle(() => { root.unmount(); root = null; });
  assert(pendingCalls.every(call => call.signal.aborted));
  await settle(() => { listGate.release(); listGate = null; });
  assert.equal(window.localStorage.getItem('serein.memory.fact-events.v2'), cachedBeforeUnmount);
  assert.equal(polls.size, 0, 'Unmount removes recurring work');
  assert.equal(deadlines.size, 0, 'Unmounted requests release their deadlines');

  window.localStorage.removeItem('serein.memory.fact-events.v2');
  fail = true;
  await settle(() => { root = mount(); });
  await click(button('事件', document.querySelector('.memory-type-switch')));
  assert.match(document.querySelector('.memory-empty[role="alert"]').textContent, /暂时没有读到/,
    'An initial failed read is not reported as a successfully empty library');
  fail = false;
  await click(refreshButton());
  assert(titles().includes('Read after reconnect'));
  records = [];
  await settle(poll);
  assert.deepEqual(titles(), [], 'A verified empty server response does clear old cached records');
  assert.match(document.querySelector('.memory-empty').textContent, /这一页还是空的/);
  await settle(() => { root.unmount(); root = null; });
  assert.equal(polls.size, 0); assert.equal(deadlines.size, 0);
  console.log('PASS Memory refresh DOM: cache validation, visible polling, return/retry/timeout, draft and selection preservation, grouped search, abort and mutation races');
} finally {
  if (root) root.unmount();
  dom.window.close();
  assert.equal(dirname(resolve(directory)), resolve(tmpdir()));
  assert(basename(directory).startsWith('serein-memory-refresh-dom-'));
  await rm(directory, { recursive: true, force: true });
}
process.exit(0);
