import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','HTMLInputElement','HTMLTextAreaElement','HTMLSelectElement','Event','CustomEvent','Node'])
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLElement.prototype.scrollTo=()=>{};
const calls=[];
let locked=false;
const feel={source_id:'scene_feel_fixture',title:'Rain feeling',date:'2026-11-08',content:'A subjective feeling.',
  memory_role:'feel',semantic_kind:'feel',revision:1,updated_at:'version-1',author:'Fixture',annotations:[],
  status:'可浮现',storage_status:'active',feel_sources:[{kind:'event',id:'event_source',status:'available',title:'STALE CACHED TITLE',summary:'STALE CACHED BODY'}]};
const memos=[{id:'memo_fixture',title:'Original reminder',content:'Keep existing memo controls.',status:'active',repeat_rule:'daily',
  daily_limit:1,reminder_count:0,max_injections:0,cooldown_minutes:0,channel:'global'}];
const reply=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>structuredClone(value)});
globalThis.fetch=window.fetch=async(url,options={})=>{
  const body=options.body?JSON.parse(options.body):{};calls.push({url,body,method:options.method});
  if(url==='/__serein/live/memory-scenes')return reply({status:'ok',scenes:[feel],edges:[]});
  if(url.startsWith('/__serein/personal'))return reply({items:[],has_more:false});
  if(url==='/__serein/settings')return reply({identity:{user_name:'User',ai_name:'Fixture'},features:{favorites:false}});
  if(url.startsWith('/__serein/live/fact-events'))return reply({items:[],count:0});
  if(url==='/__serein/companion/memos')return reply({items:memos,enabled:false});
  if(url==='/__serein/companion/memos/memo_fixture') {memos[0].status=body.status;return reply(memos[0]);}
  if(url==='/__serein/authored-types') {
    const args=body.arguments;
    if(body.tool==='read_feel')return reply({readable:true,feel_sources:[locked?{status:'restricted',kind:'event',id:'event_source'}:
      {status:'available',kind:'event',id:'event_source',title:'Verified source',summary:'Exact frozen body.',revision:1}]});
    if(body.tool==='edit_feel')return reply({document:{body_md:args.content,updated_at:'version-2',revision:2},feel_sources:[]});
  }
  throw new Error(`Unexpected synthetic request: ${url} ${body.tool||''}`);
};
const directory=await mkdtemp(join(tmpdir(),'serein-authored-ui-'));let root;
try {
  const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {MemoryPage} from './src/pages/MemoryPage.jsx';import {MemosPage} from './src/pages/CompanionPages.jsx';export {act};export function mount(name){const root=createRoot(document.getElementById('root'));root.render(name==='memory'?<MemoryPage/>:<MemosPage/>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
  const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
  const {act,mount}=await import(pathToFileURL(path));
  const wait=()=>new Promise(resolve=>setTimeout(resolve,40));
  async function settle(action){await act(async()=>{action?.();await wait();});}
  const button=(text,within=document)=>[...within.querySelectorAll('button')].find(node=>node.textContent.trim()===text);
  async function click(node){assert(node,'Element exists');assert(!node.disabled,'Element enabled');await settle(()=>node.click());}
  async function value(node,text){await settle(()=>{Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node),'value').set.call(node,text);node.dispatchEvent(new Event(node.tagName==='SELECT'?'change':'input',{bubbles:true}));});}
  await settle(()=>{root=mount('memory');});
  assert.equal(document.querySelector('.scene-feel-label').textContent,'感受');
  assert.equal(JSON.parse(window.localStorage.getItem('serein.memory.scene-records.v1'))[0].memory_role,'feel');
  await click(document.querySelector('.scene-entry__button'));
  assert(document.body.textContent.includes('感受 · 非事实证据'));
  assert(document.querySelector('.feel-sources').textContent.includes('Exact frozen body.'));
  assert(!document.body.textContent.includes('STALE CACHED'));
  assert(!document.querySelector('.scene-detail input[aria-label="记忆日期"]'));
  await click(button('编辑记忆'));await value(document.querySelector('.scene-editor textarea'),'New subjective body.');await click(button('保存'));
  assert(calls.some(row=>row.body.tool==='edit_feel'&&row.body.arguments.content==='New subjective body.'&&row.body.arguments.expected_revision===1));
  assert(!calls.some(row=>row.url==='/__serein/memory/edit-scene'));
  locked=true;await click(document.querySelector('.scene-detail__close'));await click(document.querySelector('.scene-entry__button'));
  assert(document.querySelector('.feel-sources').textContent.includes('来源当前不可读取'));
  assert(!document.querySelector('.feel-sources').textContent.includes('Exact frozen body.'));
  await settle(()=>{root.unmount();});root=null;await settle(()=>{root=mount('memos');});
  assert(!document.querySelector('.plan-notes'));
  assert(!document.body.textContent.includes('计划与承诺'));
  assert(!document.body.textContent.includes('新增计划'));
  assert.equal(document.querySelectorAll('[aria-label="备忘状态"]').length,1);
  assert(document.body.textContent.includes('Original reminder'));
  await click(button('标完成'));assert.equal(memos[0].status,'done');
  await click(button('已完成'));assert(document.body.textContent.includes('Original reminder'));
  await click(button('重新打开'));assert.equal(memos[0].status,'active');
  await click(button('进行中'));await click(button('归档'));assert.equal(memos[0].status,'archived');
  await click(button('已归档'));assert(document.body.textContent.includes('Original reminder'));
  await click(button('＋ 新增备忘'));
  assert(document.querySelector('.memo-editor textarea'));
  assert(!calls.some(row=>['list_plans','read_plan','create_plan','update_plan'].includes(row.body.tool)));
  console.log('PASS authored UI: feel semantics and fresh protected sources, typed edit, original memo controls and lifecycle, no plan UI or requests');
  await settle(()=>root.unmount());root=null;
} finally {if(root)root.unmount();dom.window.close();await rm(directory,{recursive:true,force:true});}
process.exit(0);
