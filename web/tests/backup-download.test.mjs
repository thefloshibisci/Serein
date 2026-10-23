import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { backupDownload } from '../server/backupDownload.mjs';

test('backup proxy streams before completion, keeps credentials server-side and reports failures', async () => {
  const originalFetch = globalThis.fetch;
  const oldBase = process.env.SEREIN_MEMORY_URL, oldToken = process.env.SEREIN_MEMORY_TOKEN;
  process.env.SEREIN_MEMORY_URL = 'http://synthetic.invalid';
  process.env.SEREIN_MEMORY_TOKEN = 'synthetic-secret';
  let mode = 'stream', finish;
  globalThis.fetch = async (url, options) => {
    assert.equal(url, 'http://synthetic.invalid/v1/export/backup');
    assert.equal(options.headers.Authorization, 'Bearer synthetic-secret');
    if (mode === 'failure') throw new Error('synthetic connection failure');
    if (mode === 'unauthorized') return new Response('{"detail":"Unauthorized"}', { status: 401 });
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('SQLite '));
      finish = () => { controller.enqueue(new TextEncoder().encode('backup')); controller.close(); };
    } }), { headers: { 'Content-Length': '13' } });
  };
  const server = createServer(backupDownload);
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    const response = await originalFetch(url);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal(response.headers.get('content-length'), '13');
    assert.match(response.headers.get('content-disposition'), /serein-backup.db/);
    const reader = response.body.getReader();
    assert.equal(new TextDecoder().decode((await reader.read()).value), 'SQLite ');
    finish();
    assert.equal(new TextDecoder().decode((await reader.read()).value), 'backup');
    assert.equal((await reader.read()).done, true);
    mode = 'unauthorized';
    const denied = await originalFetch(url);
    assert.equal(denied.status, 401);
    assert.equal(denied.headers.get('content-disposition'), null);
    assert.equal((await denied.json()).detail, 'Unauthorized');
    mode = 'failure';
    const failure = await originalFetch(url);
    assert.equal(failure.status, 502);
    assert.match((await failure.json()).detail, /备份未完成/);
    assert.equal((await originalFetch(url, { method: 'POST' })).status, 405);
  } finally {
    globalThis.fetch = originalFetch;
    for (const [key, value] of [['SEREIN_MEMORY_URL', oldBase], ['SEREIN_MEMORY_TOKEN', oldToken]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
});
