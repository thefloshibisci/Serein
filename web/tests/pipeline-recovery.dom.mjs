import assert from 'node:assert/strict';
import {test,after} from 'node:test';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,sep} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';

const webRoot=resolve(process.env.SEREIN_WEB_ROOT||process.cwd());
const require=createRequire(join(webRoot,'package.json'));
const {build}=require('esbuild'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','Event','Node'])
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
dom.window.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
const interval=globalThis.setInterval,clear=globalThis.clearInterval;
const keepAlive=interval(()=>{},1000);
globalThis.setInterval=()=>1;globalThis.clearInterval=()=>{};
let state,root;
const requests=[];
globalThis.fetch=async(url,options={})=>{
  requests.push({url,body:options.body?JSON.parse(options.body):null});
  let result=state;
  if(url.endsWith('/rebuild'))result={status:'rebuilt'};
  else if(url.endsWith('/retry-batch'))result={status:'resumed'};
  else if(url.endsWith('/next'))result={status:'queued',stage:'queued'};
  else assert.equal(url,'/__serein/pipeline/status');
  return {ok:true,json:async()=>structuredClone(result)};
};
const directory=await mkdtemp(join(tmpdir(),'serein-recovery-dom-'));
const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {PipelineSettings} from './src/components/PipelineSettings.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<PipelineSettings onOpenSummary={()=>{}}/>);return root;}`,resolveDir:webRoot,loader:'jsx'},
  jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"'},
  plugins:[{name:'synthetic-children',setup(build){
    build.onResolve({filter:/ConversationImport\.jsx$/},()=>({path:'imports',namespace:'synthetic'}));
    build.onResolve({filter:/instanceStore\.js$/},()=>({path:'settings',namespace:'synthetic'}));
    build.onLoad({filter:/.*/,namespace:'synthetic'},args=>({contents:args.path==='imports'?'export function ConversationImport(){return null;}':'export async function instanceSettings(){return {pipeline:{auto_enabled:true}};}',loader:'js'}));
  }}]});
const bundle=join(directory,'bundle.mjs');await writeFile(bundle,output.outputFiles[0].text);
const {act,mount}=await import(pathToFileURL(bundle));
const wait=()=>new Promise(resolve=>setTimeout(resolve,20));
const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent.trim()===text);
async function render(value){
  if(root)await act(async()=>{root.unmount();root=null;await wait();});
  state=value;requests.length=0;
  await act(async()=>{root=mount();await wait();});
}
async function click(node){assert(node,'button exists');assert(!node.disabled);await act(async()=>{node.click();await wait();});}
function held(kind='routing_evidence'){
  return {status:'needs_repair',stage:'needs_repair',result:{status:'needs_repair',batch_id:'batch-synthetic',repair_kind:kind,reason:'synthetic hold'},
    blocked_scopes:[{batch_id:'batch-synthetic',scope:'synthetic',hold_status:'needs_repair',repair_kind:kind,reason:'synthetic hold'}]};
}

test('runtime contract explains the hold and requires explicit rebuild confirmation',async()=>{
  await render(held('runtime_contract'));
  assert.match(document.body.textContent,/重新校验不会迁移旧计划/);
  assert(button('继续整理其他聊天'));assert(!button('重新校验并继续'));
  await click(button('查看重建确认'));
  assert(document.querySelector('#pipeline-rebuild-title').closest('dialog').open);
  assert.equal(requests.filter(item=>item.body).length,0);
  await click(button('确认作废旧计划并重新归线'));
  assert.deepEqual(requests.find(item=>item.url.endsWith('/rebuild')).body,{batch_id:'batch-synthetic',confirm:'REBUILD_PIPELINE_BATCH'});
});
test('runtime contract in a top-level result still permits other chats to continue',async()=>{
  const value=held('runtime_contract');delete value.blocked_scopes;
  await render(value);
  assert.match(document.body.textContent,/重新校验不会迁移旧计划/);
  await click(button('继续整理其他聊天'));
  assert(requests.some(item=>item.url.endsWith('/next')));
  assert(!requests.some(item=>item.url.endsWith('/rebuild')));
});
test('ordinary repair still revalidates without silently rebuilding',async()=>{
  await render(held());
  assert(!document.body.textContent.includes('重新校验不会迁移旧计划'));
  await click(button('重新校验并继续'));
  assert(requests.some(item=>item.url.endsWith('/next')));
  assert(!requests.some(item=>item.url.endsWith('/rebuild')));
});
test('paused failure retains its batch retry and continuation calls',async()=>{
  await render({status:'blocked',stage:'blocked',paused_batches:[{batch_id:'paused-synthetic',reason:'synthetic failure'}],
    blocked_scopes:[{batch_id:'paused-synthetic',scope:'synthetic',hold_status:'paused_failure'}]});
  assert(!button('查看重建确认'));
  await click(button('重试此批次'));
  assert.deepEqual(requests.find(item=>item.url.endsWith('/retry-batch')).body,{batch_id:'paused-synthetic'});
  assert(requests.some(item=>item.url.endsWith('/next')));
  assert(!requests.some(item=>item.url.endsWith('/rebuild')));
});
test('each runtime hold is explained when ordinary and runtime repairs coexist',async()=>{
  const value=held();value.blocked_scopes.push({batch_id:'runtime-second',scope:'other',hold_status:'needs_repair',repair_kind:'runtime_contract',reason:'synthetic old version'});
  await render(value);
  assert.match(document.body.textContent,/重新校验不会迁移旧计划/);
  assert(button('重新校验并继续'));
});
after(async()=>{
  if(root)await act(async()=>root.unmount());
  dom.window.close();globalThis.setInterval=interval;globalThis.clearInterval=clear;
  clear(keepAlive);require('esbuild').stop();
  const target=resolve(directory),parent=resolve(tmpdir());
  assert(target.startsWith(parent+sep)&&target!==parent);
  await rm(target,{recursive:true,force:true});
});
