export function appearanceBridge(server, backend, readJsonBody) {
  server.middlewares.use('/__serein/appearance/images', async (request, response) => {
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    response.setHeader('Cache-Control', 'no-store');
    const url = new URL(request.url || '/', 'http://localhost');
    const path = url.pathname;
    const slot = /^\/(user|assistant|hero)$/.test(path);
    const valid = request.method === 'GET' ? path === '/' || slot : request.method === 'PUT' && slot;
    if (!valid) { response.statusCode = 405; response.end('{}'); return; }
    if (request.method === 'PUT') {
      let sameOrigin = false;
      try { sameOrigin = !request.headers.origin || new URL(request.headers.origin).host === request.headers.host; } catch {}
      if (!sameOrigin || !String(request.headers['content-type']).startsWith('application/json')) {
        response.statusCode = 403; response.end('{}'); return;
      }
    }
    try {
      const metadata = request.method === 'GET' && path === '/' && url.searchParams.get('metadata') === '1';
      const version = request.method === 'GET' && slot ? url.searchParams.get('v') || '' : '';
      const query = metadata ? '?metadata=true' : version ? '?version='+encodeURIComponent(version) : '';
      const result = await backend('/v1/appearance/images' + (path === '/' ? '' : path) + query, {
        method: request.method,
        ...(request.method === 'PUT' ? {body: await readJsonBody(request, 3 * 1024 * 1024)} : {}),
      });
      response.statusCode = result.status;
      if (result.ok && metadata) {
        const images = Object.fromEntries(Object.entries(result.payload.images || {}).map(([key, digest]) => [key,
          `/__serein/appearance/images/${encodeURIComponent(key)}?v=${encodeURIComponent(digest)}`]));
        response.end(JSON.stringify({images})); return;
      }
      if (result.ok && request.method === 'GET' && slot) {
        const match = result.payload.data_url?.match(/^data:(image\/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)$/);
        if (!match) throw new Error('invalid_image');
        response.setHeader('Content-Type', match[1]);
        response.setHeader('X-Content-Type-Options', 'nosniff');
        if (version && version === result.payload.version) {
          response.setHeader('Cache-Control', 'private, max-age=31536000, immutable');
          response.setHeader('ETag', `"${version}"`);
        }
        response.end(Buffer.from(match[2], 'base64')); return;
      }
      response.end(JSON.stringify(result.payload));
    } catch (error) {
      response.statusCode = error.message === 'request_too_large' ? 413 : 502;
      response.end(JSON.stringify({detail: '图片未保存，请检查文件大小与后端连接后重试。'}));
    }
  });
}
