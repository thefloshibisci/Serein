import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://synthetic.invalid' });
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'Event', 'Node']) {
  Object.defineProperty(globalThis, key, { value: dom.window[key], configurable: true });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let mode = 'pending', complete, saved = 0, objectURLs = 0;
globalThis.fetch = async (url, options) => {
  if (url === '/__serein/migration') return { ok: true, json: async () => ({ items: [] }) };
  if (url === '/__serein/install-location') return { ok: false };
  assert.equal(url, '/__serein/export/backup');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.cache, 'no-store');
  if (mode === 'pending') return await new Promise(resolve => { complete = resolve; });
  if (mode === 'network') throw new TypeError('Failed to fetch');
  return { ok: false, status: 401, json: async () => ({ detail: 'Unauthorized' }) };
};
const oldCreate = URL.createObjectURL, oldRevoke = URL.revokeObjectURL;
URL.createObjectURL = blob => { assert.equal(blob.size, 13); objectURLs++; return 'blob:synthetic'; };
URL.revokeObjectURL = () => {};
dom.window.HTMLAnchorElement.prototype.click = function () {
  assert.equal(this.href, 'blob:synthetic'); assert.equal(this.download, 'serein-backup.db'); saved++;
};
const directory = await mkdtemp(join(tmpdir(), 'serein-backup-dom-'));
let root;
try {
  const output = await build({ stdin: { contents: `import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {LegacyMigration} from './src/components/LegacyMigration.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<LegacyMigration/>);return root;}`, resolveDir: process.cwd(), loader: 'jsx' }, jsx: 'automatic', bundle: true, platform: 'node', format: 'esm', write: false });
  const path = join(directory, 'bundle.mjs'); await writeFile(path, output.outputFiles[0].text);
  const { act, mount } = await import(pathToFileURL(path));
  await act(async () => { root = mount(); });
  const button = () => [...document.querySelectorAll('button')].find(node => /备份/.test(node.textContent));
  await act(async () => { button().click(); });
  assert(button().disabled); assert.match(document.body.textContent, /正在准备并接收/);
  // Avoid a real one-minute cleanup timer in this synthetic DOM test.
  const originalTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback, ms, ...args) => originalTimeout(callback, ms === 60_000 ? 1 : ms, ...args);
  try {
    await act(async () => { complete({ ok: true, blob: async () => new Blob(['SQLite backup']) }); });
  } finally { globalThis.setTimeout = originalTimeout; }
  assert.equal(saved, 1); assert.equal(objectURLs, 1); assert(!button().disabled);
  assert.match(document.body.textContent, /已请求浏览器保存/);
  mode = 'unauthorized'; await act(async () => { button().click(); });
  assert.match(document.body.textContent, /重新登录/); assert.equal(saved, 1);
  mode = 'network'; await act(async () => { button().click(); });
  assert.match(document.body.textContent, /连接中断/); assert(!button().disabled);
  await act(async () => { root.unmount(); }); root = null;
  console.log('Backup download DOM checks passed');
} finally {
  if (root) root.unmount();
  URL.createObjectURL = oldCreate; URL.revokeObjectURL = oldRevoke;
  dom.window.close(); await rm(directory, { recursive: true, force: true });
}
process.exit(0);
