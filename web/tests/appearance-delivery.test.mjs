import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {appearanceBridge} from '../server/appearanceBridge.mjs';

test('image manifest is small, images are independent and only exact versions are privately cached', async t=>{
  const digest='a'.repeat(64), image='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
  const calls=[]; let handler;
  appearanceBridge({middlewares:{use:(_,fn)=>{handler=fn;}}},async path=>{
    calls.push(path);
    if(path==='/v1/appearance/images?metadata=true') return {ok:true,status:200,payload:{images:{user:digest,hero:digest}}};
    if(path.endsWith('?version=old')) return {ok:false,status:404,payload:{detail:'Image version no longer available'}};
    return {ok:true,status:200,payload:{data_url:image,version:digest}};
  },async()=>({}));
  const server=http.createServer(handler);
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>{server.close(resolve);server.closeAllConnections();}));
  const base=`http://127.0.0.1:${server.address().port}`;
  const manifest=await fetch(base+'/?metadata=1');
  const body=await manifest.text();
  assert.ok(body.length<400);assert.ok(!body.includes('base64'));
  assert.equal(manifest.headers.get('cache-control'),'no-store');
  assert.equal(JSON.parse(body).images.user,`/__serein/appearance/images/user?v=${digest}`);
  const saved=await fetch(base+'/user?v='+digest);
  assert.equal(saved.headers.get('content-type'),'image/gif');
  assert.equal(saved.headers.get('cache-control'),'private, max-age=31536000, immutable');
  assert.deepEqual(Buffer.from(await saved.arrayBuffer()),Buffer.from(image.split(',')[1],'base64'));
  assert.equal(calls.at(-1),'/v1/appearance/images/user?version='+digest);
  const stale=await fetch(base+'/user?v=old');
  assert.equal(stale.status,404);assert.equal(stale.headers.get('cache-control'),'no-store');
  const unversioned=await fetch(base+'/user');
  assert.equal(unversioned.headers.get('cache-control'),'no-store');
});
