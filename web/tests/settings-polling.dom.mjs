import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,sep} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build,stop} from 'esbuild';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
const originals=new Map();
for(const key of ['window','document','navigator','HTMLElement','Event','Node','fetch','IS_REACT_ACT_ENVIRONMENT']){
  originals.set(key,Object.getOwnPropertyDescriptor(globalThis,key));
  if(key in dom.window)Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
}
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
let hidden=false;
Object.defineProperty(document,'hidden',{get:()=>hidden,configurable:true});
dom.window.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
dom.window.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
const interval=globalThis.setInterval,clear=globalThis.clearInterval;
const polls=new Map();let timerId=0;
globalThis.setInterval=callback=>{polls.set(++timerId,callback);return timerId;};
globalThis.clearInterval=id=>polls.delete(id);
const NativeChannel=globalThis.MessageChannel,channels=[];
globalThis.MessageChannel=class extends NativeChannel{constructor(){super();channels.push(this);}};
const requests=[],delays=new Map();
const work={status:'idle',stage:'idle',pending_materials:[],blocked_scopes:[],paused_batches:[]};
const job={id:'synthetic-upload',filename:'Synthetic history.json',format:'messages',sessions:1,total:12,
  status:'ready',processed:0,inserted:0,duplicate:0,failed:0,errors:[],warnings:[],preview:[],event_boundary_active:true};
let returnedJob=job;
const response=value=>({ok:true,json:async()=>structuredClone(value)});
globalThis.fetch=async(url,options={})=>{
  requests.push({url,method:options.method||'GET',body:options.body});
  const delayed=delays.get(url);
  if(delayed){delays.delete(url);return delayed.promise;}
  if(url==='/__serein/pipeline/status')return response(work);
  if(url==='/__serein/imports')return response({items:[returnedJob],tagging:{}});
  throw new Error('Unexpected request '+url);
};
function delay(url){
  let release;const promise=new Promise(resolve=>{release=value=>resolve(response(value));});
  delays.set(url,{promise});return release;
}
const count=url=>requests.filter(request=>request.url===url).length;
const directory=await mkdtemp(join(tmpdir(),'serein-settings-polling-'));
let root,act,render;
try{
  const result=await build({stdin:{contents:`
    import React,{act} from 'react';import {createRoot} from 'react-dom/client';
    import {PipelineSettings} from './src/components/PipelineSettings.jsx';export {act};
    export function mount(){return createRoot(document.getElementById('root'));}
    export function render(root,active){root.render(<PipelineSettings active={active} onOpenSummary={()=>{}}/>);}
  `,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,
    define:{'process.env.NODE_ENV':JSON.stringify('development')},plugins:[{name:'synthetic-settings',setup(build){
      build.onResolve({filter:/instanceStore\.js$/},()=>({path:'settings',namespace:'synthetic'}));
      build.onLoad({filter:/.*/,namespace:'synthetic'},()=>({contents:'export async function instanceSettings(){return {pipeline:{auto_enabled:true}};}',loader:'js'}));
    }}]});
  const path=join(directory,'bundle.mjs');await writeFile(path,result.outputFiles[0].text);
  const fixture=await import(pathToFileURL(path));({act,render}=fixture);root=fixture.mount();
  const settle=async(fn)=>act(async()=>{fn?.();await new Promise(resolve=>setTimeout(resolve,10));});
  const toggle=()=>[...document.querySelectorAll('label')].find(node=>node.textContent.includes('大批量导入')).querySelector('input');
  const mode=()=>[...document.querySelectorAll('label')].find(node=>node.textContent.includes('文件类型')).querySelector('select');

  await settle(()=>render(root,false));
  assert.equal(polls.size,0,'Inactive import page has no polling timers');
  assert.equal(requests.length,0,'Inactive page does not fetch status or import history');
  await settle(()=>render(root,true));
  assert.equal(polls.size,2,'Only pipeline and import-history polling are scheduled');
  assert.equal(count('/__serein/pipeline/status'),1);assert.equal(count('/__serein/imports'),1);
  await settle(()=>{mode().value='conversation';mode().dispatchEvent(new Event('change',{bubbles:true}));toggle().click();});
  assert.equal(toggle().checked,true);
  const retiredPolls=[...polls.values()];
  await settle(()=>render(root,false));
  const pausedCount=requests.length;
  await settle(()=>{for(const poll of retiredPolls)poll();});
  assert.equal(requests.length,pausedCount,'Already queued callbacks cannot restart inactive polling');
  assert.equal(polls.size,0);assert.equal(mode().value,'conversation');assert.equal(toggle().checked,true,'Leaving preserves unsaved choices');
  await settle(()=>render(root,true));
  assert.equal(requests.length,pausedCount+2,'Returning immediately refreshes each source exactly once');
  assert.equal(mode().value,'conversation');assert.equal(toggle().checked,true);

  hidden=true;await settle(()=>document.dispatchEvent(new Event('visibilitychange')));
  assert.equal(polls.size,0,'Hidden document removes both timers');
  const hiddenCount=requests.length;
  await settle(()=>document.dispatchEvent(new Event('visibilitychange')));
  assert.equal(requests.length,hiddenCount);
  hidden=false;await settle(()=>document.dispatchEvent(new Event('visibilitychange')));
  assert.equal(requests.length,hiddenCount+2);assert.equal(polls.size,2);

  const releaseStatus=delay('/__serein/pipeline/status'),releaseImports=delay('/__serein/imports');
  await settle(()=>{for(const poll of polls.values())poll();});
  const pendingCount=requests.length;
  await settle(()=>render(root,false));await settle(()=>render(root,true));
  assert.equal(requests.length,pendingCount,'Returning does not overlap still-running reads');
  await settle(()=>{for(const poll of polls.values())poll();});
  assert.equal(requests.length,pendingCount,'Repeated ticks do not overlap pending reads');
  await settle(()=>{releaseStatus({...work,stage:'stale-unselected'});releaseImports({items:[{...job,filename:'Stale inactive result.json'}],tagging:{}});});
  assert.equal(requests.length,pendingCount+2,'Each obsolete read hands off to one fresh active read');
  assert.doesNotMatch(document.body.textContent,/stale-unselected|Stale inactive result/);
  assert.equal(polls.size,2);

  const releaseHiddenStatus=delay('/__serein/pipeline/status'),releaseHiddenImports=delay('/__serein/imports');
  await settle(()=>{for(const poll of polls.values())poll();});
  const hiddenPendingCount=requests.length;
  hidden=true;await settle(()=>document.dispatchEvent(new Event('visibilitychange')));
  hidden=false;await settle(()=>document.dispatchEvent(new Event('visibilitychange')));
  assert.equal(requests.length,hiddenPendingCount,'Visibility resume also avoids overlapping reads');
  await settle(()=>{releaseHiddenStatus({...work,stage:'stale-hidden'});releaseHiddenImports({items:[{...job,filename:'Stale hidden result.json'}],tagging:{}});});
  assert.equal(requests.length,hiddenPendingCount+2,'Visibility resume immediately refreshes after pending reads finish');
  assert.doesNotMatch(document.body.textContent,/stale-hidden|Stale hidden result/);

  const releasePreview=delay('/__serein/imports/preview');
  const upload=document.querySelector('input[type="file"]');
  Object.defineProperty(upload,'files',{configurable:true,value:[{name:'New synthetic preview.txt',size:12,text:async()=>'synthetic'}]});
  await settle(()=>upload.dispatchEvent(new Event('change',{bubbles:true})));
  assert.match(document.body.textContent,/正在识别文件/);
  await settle(()=>render(root,false));
  returnedJob={...job,id:'new-synthetic',filename:'New synthetic preview.txt'};
  await settle(()=>releasePreview(returnedJob));
  assert.match(document.body.textContent,/New synthetic preview.txt/,'User action completes while its page is inactive');
  assert.match(document.body.textContent,/已识别文件/);
  assert.equal(document.querySelector('input[type="file"]').disabled,false,'Hidden completion releases busy state');
  assert.equal(polls.size,0,'Action completion does not restart background polling');
  await settle(()=>render(root,true));
  assert.match(document.body.textContent,/New synthetic preview.txt/);
  assert.equal(mode().value,'conversation');
  const releaseNext=delay('/__serein/pipeline/next');
  const next=[...document.querySelectorAll('button')].find(node=>node.textContent==='继续整理');
  await settle(()=>next.click());
  await settle(()=>render(root,false));
  await settle(()=>releaseNext({...work,status:'queued',stage:'queued'}));
  assert.match(document.body.textContent,/后台任务已提交/,'Pipeline action completes while inactive');
  assert.match(document.body.textContent,/当前阶段：等待后台处理/);
  assert.equal(polls.size,0,'Hidden pipeline action does not restart recurring reads');
  await settle(()=>root.unmount());root=null;
  assert.equal(polls.size,0,'Unmount clears recurring work');
  console.log('PASS settings polling DOM: inactive/hidden pause, immediate resume, draft retention, no overlapping reads, stale-response handoff and hidden action completion');
}finally{
  if(root)await act(async()=>root.unmount());
  dom.window.close();stop();globalThis.setInterval=interval;globalThis.clearInterval=clear;
  for(const channel of channels){channel.port1.close();channel.port2.close();}globalThis.MessageChannel=NativeChannel;
  for(const [key,descriptor] of originals)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
  assert(resolve(directory).startsWith(resolve(tmpdir())+sep));await rm(directory,{recursive:true,force:true});
}
