import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://synthetic.invalid/#narrative', pretendToBeVisual: true });
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'Event', 'CustomEvent', 'Node']) {
  Object.defineProperty(globalThis, key, { value: dom.window[key], configurable: true });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
let confirm = false, stale = true, renameStale = true, shelfReads = 0;
window.confirm = () => confirm;
let books = [{ narrative_id: 'narrative_synthetic', title: '合成验收卷', body: '', revision: 1,
  document_sha256: 'a'.repeat(64), publication_status: 'collecting', lifecycle: 'active',
  linked_event_ids: ['event_1', 'event_2'] }];
const sources = [{ source_type: 'event', source_id: 'event_1', title: '合成材料一', receipt: 'a'.repeat(64) },
  { source_type: 'event', source_id: 'event_2', title: '合成材料二', receipt: 'b'.repeat(64) }];
const calls = [];
globalThis.fetch = async (url, options) => {
  calls.push(url);
  const body = options?.body ? JSON.parse(options.body) : {};
  let payload, status = 200;
  if (url === '/__serein/live/narratives') {
    shelfReads++;
    payload = { status: 'ok', items: books };
  } else if (url === '/__serein/narrative-preview') {
    status = 502; payload = { status: 'error', reason: 'narrative_writer_auth_failed', message: 'Writer 登录已失效。' };
  } else if (url === '/__serein/narrative-rename') {
    if (renameStale) { status = 409; payload = { status: 'conflict', reason: 'narrative_revision_changed' }; }
    else {
      books[0] = { ...books[0], title: body.title, revision: books[0].revision + 1 };
      payload = { ...books[0], status: 'updated' };
    }
  } else if (url === '/__serein/narrative-delete') {
    assert.equal(body.confirm, 'DELETE');
    if (stale) { status = 409; payload = { status: 'conflict', reason: 'narrative_revision_changed' }; }
    else { books = []; payload = { status: 'deleted' }; }
  } else if (url === '/__serein/narrative-discover-theme') {
    payload = { status: 'ok', title: '新合成卷', outline: '合成材料测试。', sources };
  } else if (url === '/__serein/narrative-create-line') {
    payload = { status: 'created', narrative_id: 'narrative_new', narrative: { ...books[0],
      narrative_id: 'narrative_new', title: '新合成卷', publication_status: 'collecting',
      revision: 1, body: '', linked_event_ids: ['event_1', 'event_2'] } };
  } else throw new Error(`Unexpected request ${url}`);
  return new Response(JSON.stringify(payload), { status });
};
const directory = await mkdtemp(join(tmpdir(), 'serein-narrative-dom-'));
let root;
try {
  const bundle = await build({ stdin: { contents: `import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {NarrativePage} from './src/pages/NarrativePage.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<NarrativePage/>);return root;}`,
    resolveDir: process.cwd(), loader: 'jsx' }, jsx: 'automatic', bundle: true, platform: 'node', format: 'esm', write: false,
    define: { 'process.env.NODE_ENV': '"development"' } });
  const path = join(directory, 'bundle.mjs'); await writeFile(path, bundle.outputFiles[0].text);
  const { act, mount } = await import(pathToFileURL(path));
  const flush = () => new Promise(resolve => setTimeout(resolve, 15));
  const button = text => [...document.querySelectorAll('button')].find(item => item.textContent.trim() === text);
  const click = async item => { assert(item); await act(async () => { item.click(); await flush(); }); };
  await act(async () => { root = mount(); await flush(); });
  await click(document.querySelector('.narrative-book'));
  await click(button('改书名'));
  const titleInput = document.querySelector('#narrative-book-title');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(titleInput, '改名后的合成卷');
    titleInput.dispatchEvent(new window.Event('input', { bubbles: true })); await flush();
  });
  await click(button('保存书名'));
  assert.match(document.body.textContent, /修改书名失败.*已有新修改/);
  assert.equal(titleInput.value, '改名后的合成卷');
  renameStale = false;
  await click(button('保存书名'));
  assert.equal(document.querySelector('#narrative-reader-title').textContent, '改名后的合成卷');
  await click(button('回到书架'));
  assert.match(document.querySelector('.narrative-book').getAttribute('aria-label'), /改名后的合成卷/);
  await click(document.querySelector('.narrative-book'));
  await click(button('书写'));
  assert.match(document.body.textContent, /Writer 登录已失效/);
  assert.match(document.body.textContent, /叙事线和材料已保存/);
  const deletesBefore = calls.filter(url => url.endsWith('narrative-delete')).length;
  await click(button('删除'));
  assert.equal(calls.filter(url => url.endsWith('narrative-delete')).length, deletesBefore);
  confirm = true;
  await click(button('删除'));
  assert.match(document.body.textContent, /已有新修改/);
  assert(document.querySelector('.narrative-reader'));
  stale = false;
  await click(button('删除'));
  assert(!document.querySelector('.narrative-reader'));
  assert.equal(document.querySelectorAll('.narrative-book:not(.narrative-book--future)').length, 0);
  await click(document.querySelector('.narrative-book--future'));
  const textarea = document.querySelector('textarea');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(textarea, '合成主题');
    textarea.dispatchEvent(new window.Event('input', { bubbles: true })); await flush();
  });
  await click(button('找材料'));
  const readsBefore = shelfReads;
  await click(button('书写'));
  assert.equal(shelfReads, readsBefore, 'Newly saved book opens without rereading the whole shelf');
  assert.equal(document.querySelector('#narrative-reader-title').textContent, '新合成卷');
  assert.match(document.body.textContent, /叙事线和材料已保存/);
  assert.equal(document.querySelectorAll('.narrative-create').length, 0);
  console.log('NARRATIVE_FLOW_DOM_OK');
} finally {
  if (root) {
    const { act } = await import(pathToFileURL(join(directory, 'bundle.mjs')));
    await act(async () => root.unmount());
  }
  await rm(directory, { recursive: true, force: true });
  dom.window.close();
}
// React's jsdom scheduler can retain a MessagePort after unmounting.
process.exit(0);
