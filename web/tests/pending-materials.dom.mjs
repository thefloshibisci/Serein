import assert from 'node:assert/strict';
import {test,after} from 'node:test';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
const webRoot=resolve(process.cwd()),require=createRequire(join(webRoot,'package.json'));
const {build,stop}=require('esbuild'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'https://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','Event','Node'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new Event('close'));};
const channels=[],NativeChannel=globalThis.MessageChannel;
globalThis.MessageChannel=class extends NativeChannel{constructor(){super();channels.push(this);}};
const directory=await mkdtemp(join(tmpdir(),'serein-pending-dom-'));
const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {PendingMaterials} from './src/components/PendingMaterials.jsx';export {act};export function update(root,work,props={}){root.render(<PendingMaterials work={work} {...props}/>);}export function mount(work,props){const root=createRoot(document.getElementById('root'));update(root,work,props);return root;}`,resolveDir:webRoot,loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"'}});
const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
const {act,mount,update}=await import(pathToFileURL(path));let root,busy=false,skipped=false,archived=false;const requests=[];
const item={id:17,batch_id:'synthetic',status:'open',kind:'writing',reason:'Synthetic writer failure',summary:'Book club',originals:2,original_chars:100,can_skip:true,
  locator:{source:'synthetic',session_id:'book-chat',first_at:'2025-01-01T00:00:00Z',last_at:'2025-01-01T00:01:00Z',first_raw_id:101,last_raw_id:102,preview:'FIRST_SNIPPET',longest_message:{id:102,role:'assistant',chars:2000}},
  resolved_event_ids:[],last_attempt:{role:'event_writer:0:0',error:'Synthetic error',output_chars:20}};
globalThis.fetch=async(url,options={})=>{
  requests.push({url,body:options.body?JSON.parse(options.body):null});let result;
  if(url.endsWith('/pending/17'))result={id:17,batch_id:'synthetic',messages:[{id:101,role:'user',content:'SYNTHETIC_ORIGINAL'},
    {id:102,role:'assistant',content:'L'.repeat(2000)}],last_attempt:{output_text:'SYNTHETIC_BAD_OUTPUT'}};
  else if(url.endsWith('/resume-pending'))result={status:busy?'busy':'resumed'};
  else if(url.endsWith('/next'))result={status:'queued'};
  else if(url.endsWith('/status'))result={pending_materials:[{...item,status:archived?'archived_only':skipped?'resolved_skipped':'resolved',resolved_event_ids:skipped||archived?[]:['event-synthetic']}]};
  else if(url.endsWith('/recovery-preview'))result={pending_id:17,batch_id:'synthetic',originals:2,original_chars:100,max_input_chars:5000,batches:2,oversized_batches:0,can_rebatch:true};
  else if(url.endsWith('/rebatch'))result={status:'rebatched'};
  else if(url.endsWith('/skip-batch')){skipped=true;result={status:'skipped_batch',skipped_originals:2};}
  else if(url.endsWith('/archive-pending')){archived=true;result={status:'archived_only',pending_id:17,originals:2,closure_receipt:{reason:'manual'}};}
  else throw new Error('Unexpected request '+url);
  return {ok:true,json:async()=>structuredClone(result)};
};
const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent===text);
async function render(status){if(root)await act(async()=>root.unmount());requests.length=0;skipped=false;archived=false;await act(async()=>{root=mount({status,pending_materials:[item]});});}
async function click(text){await act(async()=>{button(text).click();});}
after(async()=>{if(root)await act(async()=>root.unmount());dom.window.close();stop();for(const c of channels){c.port1.close();c.port2.close();}globalThis.MessageChannel=NativeChannel;await rm(directory,{recursive:true,force:true});});
test('pending summaries do not fetch originals until explicitly opened',async()=>{
  await render();assert.match(document.body.textContent,/已切分待补写/);assert(!requests.length);
  assert.match(document.body.textContent,/book-chat/);assert.match(document.body.textContent,/raw:101/);assert.match(document.body.textContent,/FIRST_SNIPPET/);
  assert(!document.body.textContent.includes('SYNTHETIC_ORIGINAL'));
  await click('查看原话与最后输出');assert.match(document.body.textContent,/SYNTHETIC_BAD_OUTPUT/);assert.match(document.body.textContent,/SYNTHETIC_ORIGINAL/);
  assert.equal(document.querySelectorAll('.pending-originals article').length,2);
  assert.equal(document.querySelector('.pending-originals article pre').textContent,'SYNTHETIC_ORIGINAL');
});
test('continuing a pending item sends its id and starts work only after resume',async()=>{
  await render();await click('从失败步骤继续');assert.deepEqual(requests[0].body,{pending_id:17,replan:false});assert(requests.some(r=>r.url.endsWith('/next')));
  assert.equal(document.querySelectorAll('.pending-material').length,0);assert.match(document.body.textContent,/暂无待处理材料/);
  assert(!document.body.textContent.includes('event-synthetic'));assert(!document.body.textContent.includes('已处理记录'));
});
test('a busy pipeline does not start another task',async()=>{
  await render();busy=true;await click('从失败步骤继续');busy=false;assert(!requests.some(r=>r.url.endsWith('/next')));assert.match(document.body.textContent,/整理正在运行/);
});
test('smaller batches require preview and an explicit confirmation',async()=>{
  await render();await click('按更小材料重新整理');assert(!requests.some(r=>r.url.endsWith('/rebatch')));await click('确认拆小并整理');assert.deepEqual(requests.find(r=>r.url.endsWith('/rebatch')).body,{batch_id:'synthetic',max_input_chars:5000,confirm:'REBATCH_PIPELINE_BATCH'});
});

test('locating the longest original opens its complete text and focuses the correct message',async()=>{
  await render();await click('定位这条原话');
  assert.equal(document.activeElement.dataset.rawId,'102');
  const message=document.querySelector('[data-raw-id="102"]');
  assert.equal(message.querySelector('details').open,true);
  assert.equal(message.querySelector('pre').textContent.length,2000);
});

test('skipping is limited to the selected batch and requires a preview plus confirmation',async()=>{
  await render();await click('跳过本批，保留原话');
  assert(!requests.some(r=>r.url.endsWith('/skip-batch')));
  await click('取消');assert(!requests.some(r=>r.url.endsWith('/skip-batch')));
  await click('跳过本批，保留原话');assert.equal(document.querySelector('.pending-finish-dialog').open,true);await click('确认结束整理');
  assert.deepEqual(requests.find(r=>r.url.endsWith('/skip-batch')).body,{batch_id:'synthetic',confirm:'SKIP_PIPELINE_BATCH'});
  assert.match(document.body.textContent,/已结束这批 2 条原话的整理，原话保留/);
  assert.equal(document.querySelectorAll('.pending-material').length,0);assert(!document.body.textContent.includes('已处理记录'));
  assert(!requests.some(r=>r.url.endsWith('/next')));
});

test('archive-only finishes done-batch leftovers with a visible confirmation and no new model work',async()=>{
  await render();await act(async()=>{root.unmount();root=mount({status:'idle',pending_materials:[{...item,can_skip:false,can_archive:true,plan_compatible:false}]});});
  await click('只保留原话');assert.equal(document.querySelector('.pending-finish-dialog').open,true);
  assert(!requests.some(r=>r.url.endsWith('/archive-pending')));
  await click('取消');assert.equal(document.querySelector('.pending-finish-dialog').open,false);
  await click('只保留原话');await click('确认结束整理');
  assert.deepEqual(requests.find(r=>r.url.endsWith('/archive-pending')).body,{pending_id:17,confirm:'ARCHIVE_PENDING_ORIGINALS'});
  assert(!document.querySelector('.pending-material'));assert.match(document.body.textContent,/已结束整理，原话保留/);
  assert(!requests.some(r=>r.url.endsWith('/next')));
});

test('confirmed finishes survive late work props and stale refreshes without hiding other pending items',async()=>{
  const originalFetch=globalThis.fetch;
  for(const action of ['archive','skip']){
    await render();
    const selected={...item,can_archive:action==='archive'},other={...item,id:18,batch_id:'another-batch'};
    const stale={status:'idle',pending_materials:[selected,other]},changes=[];
    const props={onChange:value=>changes.push(value)};
    await act(async()=>update(root,stale,props));
    globalThis.fetch=async(url,options)=>url.endsWith('/status')?{ok:true,json:async()=>structuredClone(stale)}:originalFetch(url,options);
    await click(action==='archive'?'只保留原话':'跳过本批，保留原话');await click('确认结束整理');
    assert.equal(changes.at(-1).pending_materials[0].status,action==='archive'?'archived_only':'resolved_skipped');
    await act(async()=>update(root,structuredClone(stale),props));
    assert.deepEqual([...document.querySelectorAll('.pending-material')].map(node=>node.getAttribute('aria-label')),['待办 18']);
    assert(!requests.some(request=>request.url.endsWith('/next')));
    globalThis.fetch=originalFetch;
  }
});

test('a standalone poll started before archiving cannot undo its terminal state or notify the parent with stale work',async()=>{
  await render();await act(async()=>root.unmount());root=null;
  const originalFetch=globalThis.fetch,originalInterval=globalThis.setInterval,originalClear=globalThis.clearInterval;
  const stale={status:'idle',pending_materials:[{...item,can_archive:true}]},observed=[];
  let poll,releasePoll,delay=false,done=false;
  globalThis.setInterval=callback=>{poll=callback;return 1;};globalThis.clearInterval=()=>{};
  globalThis.fetch=async(url,options)=>{
    if(url.endsWith('/status')){
      if(delay){delay=false;return new Promise(resolve=>{releasePoll=()=>resolve({ok:true,json:async()=>structuredClone(stale)});});}
      return {ok:true,json:async()=>({...stale,pending_materials:[{...stale.pending_materials[0],status:done?'archived_only':'open'}]})};
    }
    const result=await originalFetch(url,options);if(url.endsWith('/archive-pending'))done=true;return result;
  };
  try{
    await act(async()=>{root=mount(undefined,{onStatus:value=>observed.push(value)});});
    delay=true;await act(async()=>{poll();});
    await click('只保留原话');await click('确认结束整理');
    await act(async()=>releasePoll());
    assert.equal(document.querySelectorAll('.pending-material').length,0);
    assert.equal(observed.at(-1).pending_materials[0].status,'archived_only');
    assert(!requests.some(request=>request.url.endsWith('/next')));
  }finally{
    if(root)await act(async()=>root.unmount());root=null;
    globalThis.fetch=originalFetch;globalThis.setInterval=originalInterval;globalThis.clearInterval=originalClear;
  }
});

test('standalone polling pauses when inactive or hidden and preserves confirmed finishes on return',async()=>{
  if(root)await act(async()=>root.unmount());root=null;
  const originalFetch=globalThis.fetch,originalInterval=globalThis.setInterval,originalClear=globalThis.clearInterval;
  let hidden=false,nextTimer=0,reads=0,delay=false,releasePoll;
  const timers=new Map(),observed=[];
  const onStatus=value=>observed.push(value);
  const pending={status:'idle',pending_materials:[{...item,can_archive:true}]};
  Object.defineProperty(document,'hidden',{get:()=>hidden,configurable:true});
  globalThis.setInterval=callback=>{timers.set(++nextTimer,callback);return nextTimer;};
  globalThis.clearInterval=id=>timers.delete(id);
  globalThis.fetch=async(url,options)=>{
    if(url.endsWith('/status')){
      reads++;
      if(delay){delay=false;return new Promise(resolve=>{releasePoll=()=>resolve({ok:true,json:async()=>structuredClone(pending)});});}
      // Deliberately keep returning stale open data after a successful archive.
      return {ok:true,json:async()=>structuredClone(pending)};
    }
    return originalFetch(url,options);
  };
  try{
    await act(async()=>{root=mount(undefined,{active:false,onStatus});});
    assert.equal(reads,0);assert.equal(timers.size,0);
    await act(async()=>update(root,undefined,{active:true,onStatus}));
    assert.equal(reads,1);assert.equal(timers.size,1);
    const retired=[...timers.values()][0];
    await act(async()=>update(root,undefined,{active:false,onStatus}));
    await act(async()=>retired());
    assert.equal(reads,1,'Queued callbacks cannot poll an inactive panel');assert.equal(timers.size,0);
    await act(async()=>update(root,undefined,{active:true,onStatus}));
    assert.equal(reads,2);
    hidden=true;await act(async()=>document.dispatchEvent(new Event('visibilitychange')));
    assert.equal(timers.size,0);assert.equal(reads,2);
    hidden=false;await act(async()=>document.dispatchEvent(new Event('visibilitychange')));
    assert.equal(timers.size,1);assert.equal(reads,3);
    delay=true;await act(async()=>{[...timers.values()][0]();});
    assert.equal(reads,4);
    await act(async()=>update(root,undefined,{active:false,onStatus}));
    await act(async()=>update(root,undefined,{active:true,onStatus}));
    assert.equal(reads,4,'Resuming waits for an outstanding read');
    await click('只保留原话');await click('确认结束整理');
    assert.equal(document.querySelectorAll('.pending-material').length,0);
    await act(async()=>releasePoll());
    assert.equal(reads,6,'Old read completion starts exactly one current poll');
    assert.equal(observed.at(-1).pending_materials[0].status,'archived_only');
    assert.equal(document.querySelectorAll('.pending-material').length,0,'Stale reads and resume cannot revive a confirmed finish');
    delay=true;await act(async()=>{[...timers.values()][0]();});
    assert.equal(reads,7);
    hidden=true;await act(async()=>document.dispatchEvent(new Event('visibilitychange')));
    hidden=false;await act(async()=>document.dispatchEvent(new Event('visibilitychange')));
    assert.equal(reads,7,'Visibility resume does not overlap a pending read');
    const beforeReturn=observed.length;
    await act(async()=>releasePoll());
    assert.equal(reads,8,'Visibility resume refreshes immediately after old read finishes');
    assert.equal(observed.length,beforeReturn+1,'Only the fresh visibility response is accepted');
    assert.equal(observed.at(-1).pending_materials[0].status,'archived_only');
    await act(async()=>update(root,{status:'idle',pending_materials:pending.pending_materials},{active:false,onStatus}));
    assert.equal(timers.size,0,'Parent-supplied work never starts standalone polling');
    assert.equal(document.querySelectorAll('.pending-material').length,0);
  }finally{
    if(root)await act(async()=>root.unmount());root=null;
    delete document.hidden;
    globalThis.fetch=originalFetch;globalThis.setInterval=originalInterval;globalThis.clearInterval=originalClear;
  }
});

test('an archive error stays in the visible dialog and does not claim completion',async()=>{
  await render();await act(async()=>update(root,{status:'idle',pending_materials:[{...item,can_archive:true}]}));
  const originalFetch=globalThis.fetch;
  globalThis.fetch=async(url,options)=>url.endsWith('/archive-pending')?{ok:false,json:async()=>({detail:'Synthetic shared dependency'})}:originalFetch(url,options);
  try{
    await click('只保留原话');await click('确认结束整理');
    const dialog=document.querySelector('.pending-finish-dialog');assert(dialog.open);
    assert.match(dialog.querySelector('[role="alert"]').textContent,/Synthetic shared dependency/);
    assert.equal(document.querySelectorAll('.pending-material').length,1);
    assert(!requests.some(request=>request.url.endsWith('/next')));
  }finally{globalThis.fetch=originalFetch;}
});

test('a running task still permits material inspection while preventing competing recovery actions',async()=>{
  await render('running');
  for(const label of ['从失败步骤继续','重新切分，保留归线','按更小材料重新整理','跳过本批，保留原话'])assert.equal(button(label).disabled,true);
  await click('查看原话与最后输出');assert.equal(requests.length,1);
});

test('unfinished short chats stay actionable in one list until explicitly finished',async()=>{
  const parked={...item,id:18,batch_id:'parked',waiting_for_continuation:true,plan_compatible:true,reason:'等待后续对话',can_archive:true};
  if(root)await act(async()=>root.unmount());requests.length=0;
  await act(async()=>{root=mount({status:'idle',pending_materials:[parked]});});
  assert.match(document.body.textContent,/有 1 批材料待处理/);assert(!document.body.textContent.includes('暂无待处理材料'));
  assert.equal(document.querySelector('.pending-materials__waiting'),null);
  assert(!document.body.textContent.includes('等后续'));assert(!document.body.textContent.includes('等待后续'));assert(!document.body.textContent.includes('不需要你处理'));
  assert.match(document.querySelector('.pending-material h4').textContent,/待办 18 · 已切分待补写/);
  assert(button('从失败步骤继续'));assert(button('重新切分，保留归线'));
  assert(button('只保留原话'));assert.match(document.body.textContent,/MCP/);
  await act(async()=>{root.unmount();root=mount({status:'idle',pending_materials:[parked,item]});});
  assert.match(document.body.textContent,/有 2 批材料待处理/);assert.equal(document.querySelectorAll('.pending-materials > .pending-material').length,2);
});

test('a plan frozen by an older build offers only a rebuild, not a continue that must fail',async()=>{
  if(root)await act(async()=>root.unmount());requests.length=0;
  await act(async()=>{root=mount({status:'idle',pending_materials:[{...item,plan_compatible:false}]});});
  assert.equal(button('从失败步骤继续'),undefined);assert.match(document.body.textContent,/更早的程序版本/);
  await click('重新切分，保留归线');assert.deepEqual(requests[0].body,{pending_id:17,replan:true});
});

test('finished receipts do not appear as another list or count as pending',async()=>{
  if(root)await act(async()=>root.unmount());
  await act(async()=>{root=mount({status:'idle',pending_materials:[{...item,waiting_for_continuation:false},
    {...item,id:18,waiting_for_continuation:true},
    {...item,id:19,status:'archived_only',summary:'FINISHED_ORIGINAL_ONLY'},
    {...item,id:20,status:'resolved',summary:'FINISHED_EVENT',resolved_event_ids:['event-finished']}]});});
  assert.match(document.body.textContent,/有 2 批材料待处理/);
  assert.equal(document.querySelector('.pending-materials > .pending-material').getAttribute('aria-label'),'待办 17');
  assert(button('从失败步骤继续'));
  assert.equal(document.querySelectorAll('.pending-material').length,2);
  for(const text of ['已处理记录','FINISHED_ORIGINAL_ONLY','FINISHED_EVENT','event-finished'])assert(!document.body.textContent.includes(text));
  await act(async()=>update(root,{status:'idle',pending_materials:[{...item,status:'archived_only'}]}));
  assert.match(document.body.textContent,/暂无待处理材料/);assert.equal(document.querySelectorAll('.pending-material').length,0);
});

test('MCP completion removes open details and invalidates an outstanding original read',async()=>{
  await render();await click('查看原话与最后输出');
  assert(document.querySelector('.pending-material-detail'));
  await act(async()=>update(root,{status:'idle',pending_materials:[{...item,status:'archived_only'}]}));
  assert.equal(document.querySelector('.pending-material-detail'),null);
  assert.equal(document.querySelectorAll('.pending-material').length,0);
  await render();
  const originalFetch=globalThis.fetch;let resolveRead;
  globalThis.fetch=(url,options)=>url.endsWith('/pending/17')?new Promise(resolve=>{resolveRead=()=>resolve({ok:true,json:async()=>({id:17,status:'open',messages:[{id:101,content:'STALE_ORIGINAL'}]})});}):originalFetch(url,options);
  try{
    await act(async()=>{button('查看原话与最后输出').click();});
    await act(async()=>update(root,{status:'idle',pending_materials:[]}));
    await act(async()=>resolveRead());
    assert.equal(document.querySelector('.pending-material-detail'),null);
    assert(!document.body.textContent.includes('STALE_ORIGINAL'));
    assert.match(document.body.textContent,/暂无待处理材料/);
  }finally{globalThis.fetch=originalFetch;}
});

test('MCP completion dismisses a pending confirmation instead of leaving a finished task behind',async()=>{
  await render();await act(async()=>update(root,{status:'idle',pending_materials:[{...item,can_archive:true}]}));
  await click('只保留原话');assert(document.querySelector('.pending-finish-dialog').open);
  await act(async()=>update(root,{status:'idle',pending_materials:[]}));
  assert.equal(document.querySelector('.pending-finish-dialog').open,false);
  assert.equal(document.querySelectorAll('.pending-material').length,0);
  assert(!requests.some(request=>request.url.endsWith('/archive-pending')));
});
