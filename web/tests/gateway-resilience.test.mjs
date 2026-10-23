import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { scryptSync } from 'node:crypto';
import { once } from 'node:events';

async function freePort() {
  const server = http.createServer();
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve)); return port;
}

async function gateway(t) {
  const dir = mkdtempSync(join(tmpdir(), 'serein-resilience-'));
  const salt = '1234567890abcdef';
  writeFileSync(join(dir, 'auth.json'), JSON.stringify({ username: 'fixture', salt,
    hash: scryptSync('synthetic-password', Buffer.from(salt, 'hex'), 32).toString('hex') }));
  writeFileSync(join(dir, 'token'), 'synthetic-token');
  const core = http.createServer((req, res) => res.end('{}'));
  core.listen(0, '127.0.0.1'); await once(core, 'listening');
  const port = await freePort(), preview = await freePort();
  const child = spawn(process.execPath, ['server/gateway.mjs'], {
    cwd: new URL('../', import.meta.url), stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, SEREIN_MEMORY_URL: `http://127.0.0.1:${core.address().port}`,
      SEREIN_MEMORY_TOKEN_FILE: join(dir, 'token'), SEREIN_WEB_AUTH_FILE: join(dir, 'auth.json'),
      SEREIN_GATEWAY_PORT: String(port), SEREIN_GATEWAY_BIND: '127.0.0.1',
      SEREIN_PREVIEW_PORT: String(preview), SEREIN_PUBLIC_ORIGIN: '' } });
  let output = '';
  child.stdout.on('data', d => output += d); child.stderr.on('data', d => output += d);
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) { child.kill(); await once(child, 'exit'); }
    core.closeAllConnections(); await new Promise(resolve => core.close(resolve));
    rmSync(dir, { recursive: true, force: true });
  });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try { await fetch(base); return { port, base, child, output: () => output }; }
    catch { if (child.exitCode !== null) throw new Error(output); await new Promise(r => setTimeout(r, 100)); }
  }
  throw new Error(`Gateway did not start: ${output}`);
}

test('malformed request targets return 400 and leave gateway serving requests', { timeout: 20000 }, async t => {
  const fixture = await gateway(t);
  for (const target of ['//', '//[', 'http://[']) {
    const response = await new Promise((resolve, reject) => {
      const conn = net.createConnection({ host: '127.0.0.1', port: fixture.port });
      let data = '';
      conn.setTimeout(3000, () => conn.destroy(new Error('Request timed out')));
      conn.on('connect', () => conn.write(`GET ${target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n`));
      conn.on('data', chunk => data += chunk);
      conn.on('error', error => reject(new Error(`${error.message}\n${fixture.output()}`)));
      conn.on('end', () => resolve(data));
    });
    assert.match(response, /^HTTP\/1\.1 400 /, fixture.output());
    assert.equal((await fetch(fixture.base + '/ready')).status, 200);
    assert.equal(fixture.child.exitCode, null);
  }
});

test('missing and incorrect web credentials return typed non-sniffable 401 text', { timeout: 20000 }, async t => {
  const { base } = await gateway(t);
  for (const headers of [{}, { Authorization: 'Basic ' + Buffer.from('fixture:wrong').toString('base64') }]) {
    const response = await fetch(base, { headers });
    assert.equal(response.status, 401);
    assert.equal(response.headers.get('content-type'), 'text/plain; charset=utf-8');
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.match(response.headers.get('www-authenticate'), /^Basic /);
    assert.equal(await response.text(), 'Authentication required');
  }
});
