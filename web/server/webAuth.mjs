import { readFileSync } from 'node:fs';
import { scryptSync, timingSafeEqual } from 'node:crypto';

export function authenticate(header, record) {
  if (!header?.startsWith('Basic ')) return false;
  const text = Buffer.from(header.slice(6), 'base64').toString('utf8');
  const colon = text.indexOf(':');
  if (colon < 0 || text.slice(0, colon) !== record.username) return false;
  const expected = Buffer.from(record.hash, 'hex');
  const actual = scryptSync(text.slice(colon + 1), Buffer.from(record.salt, 'hex'), 32);
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

export function requireWebAuth(request, response, filename) {
  try {
    if (authenticate(request.headers.authorization, JSON.parse(readFileSync(filename, 'utf8')))) return true;
  } catch {
    response.writeHead(503, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end('页面鉴权尚未配置，请运行管理脚本的选项 0。');
    return false;
  }
  response.writeHead(401, { 'WWW-Authenticate': 'Basic realm="Serein", charset="UTF-8"', 'Cache-Control': 'no-store',
    'Content-Type': 'text/plain; charset=utf-8', 'X-Content-Type-Options': 'nosniff' });
  response.end('Authentication required');
  return false;
}
