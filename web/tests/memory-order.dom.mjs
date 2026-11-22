// All requests are synthetic and intercepted; this script never reaches a backend.
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {basename,dirname,join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', {url:'http://synthetic.invalid',pretendToBeVisual:true});
for (const key of ['window','document','navigator','HTMLElement','HTMLInputElement','HTMLTextAreaElement','Event','CustomEvent','Node']) {
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLElement.prototype.scrollTo = () => {};
dom.window.HTMLElement.prototype.scrollIntoView = () => {};

const scene = (id,title,date,memoryTime,extra={}) => ({
  source_id:id,title,date,content:`Synthetic body for ${title}`,author:'Fixture',
  memory_time:memoryTime,memory_time_basis:'created',recorded_at:memoryTime,
  status:'可浮现',storage_status:'active',type:'scene',active:true,scene_status:'active',
  status_consistent:true,updated_at:'synthetic-version-1',scene_cues:['synthetic'],
  annotations:[],...extra,
});
// Same-day IDs intentionally oppose their time order.
const scenes = [
  scene('scene_zzz_early','Match early','2026-11-08','2026-11-08T02:00:00Z'),
  scene('scene_zzz_unknown','Unknown original time','','',{memory_time_basis:'unknown'}),
  scene('scene_zzz_prior','Prior dated memory','2026-11-07','2026-11-07',{
    memory_time_basis:'date',recorded_at:'2026-11-09T04:00:00+08:00',
  }),
  scene('scene_mmm_feel','Feeling after midnight','2026-11-09','2026-11-08T18:30:00Z',{
    memory_time_basis:'authored_at',memory_role:'feel',semantic_kind:'feel',feel_sources:[],
  }),
  scene('scene_aaa_late','Match late','2026-11-08','2026-11-08T18:00:00+08:00'),
];
const events = [
  {item_id:'event_recent_start',item_type:'event',title:'Recent start',body:'Synthetic recent event.',
    status:'active',local_date:'2026-11-09',local_start_time:'22:00',local_end_date:'2026-11-09',local_end_time:'23:00'},
  {item_id:'event_spanning_days',item_type:'event',title:'Latest end',body:'Synthetic spanning event.',
    status:'active',local_date:'2026-11-05',local_start_time:'23:00',local_end_date:'2026-11-10',local_end_time:'01:00'},
];

const cachedScenes = scenes.map(({memory_time:_time,memory_time_basis:_basis,recorded_at:_recorded,...item}) => ({
  id:item.source_id,title:item.title,date:item.date,body:[item.content],excerpt:item.content,
  author:item.author,annotations:[],sources:[{id:`manual_source:${item.source_id}`}],
  sourceCount:1,relatedScenes:[],relatedSceneIds:[],relationCount:0,narrativeRefs:[],
  status:'可浮现',favorite:false,sourceKind:'serein-live-readonly',canonicalSceneId:item.source_id,
  sourceUpdatedAt:item.updated_at,
}));
window.localStorage.setItem('serein.personal.imported.v1','1');
window.localStorage.setItem('serein.memory.scene-records.v1',JSON.stringify(cachedScenes));
window.localStorage.setItem('serein.memory.live-cache-at.v1',String(Date.now()));

const calls = [];
let releaseProjection;
const projectionGate = new Promise(resolve => {releaseProjection=resolve;});
const reply = (value,status=200) => ({ok:status>=200&&status<300,status,json:async()=>structuredClone(value)});
globalThis.fetch = window.fetch = async (input,options={}) => {
  const path=String(input);const body=options.body?JSON.parse(options.body):{};
  calls.push({path,body});
  if(path==='/__serein/live/memory-scenes') {
    await projectionGate;
    return reply({status:'ok',scenes,edges:[]});
  }
  if(path.startsWith('/__serein/personal')) return reply({items:[],has_more:false});
  if(path==='/__serein/settings') return reply({identity:{user_name:'User',ai_name:'Fixture'},
    features:{favorites:false},tagging:{domains:[{key:'general',label:'General'}]}});
  if(path==='/__serein/live/fact-events') {
    const items=body.type==='event'&&body.status==='active'?events:[];
    return reply({items,count:items.length});
  }
  if(path==='/__serein/memory/scene-evidence') return reply({evidence_refs:[]});
  if(path==='/__serein/memory/read-scene') {
    const item=scenes.find(row=>row.source_id===body.sceneId);
    return reply({content:item.content,metadata:{updated_at:item.updated_at,scene_cues:item.scene_cues}});
  }
  if(path==='/__serein/memory/edit-scene') {
    const item=scenes.find(row=>row.source_id===body.sceneId);
    assert(item,'Edited Scene exists');
    assert.equal(body.expectedUpdatedAt,item.updated_at);
    item.date=body.date;item.memory_time=body.date;item.memory_time_basis='date';
    item.updated_at='synthetic-version-2';
    return reply({status:'updated',updated_at:item.updated_at});
  }
  throw new Error(`Unexpected synthetic request: ${path}`);
};

const directory=await mkdtemp(join(tmpdir(),'serein-memory-order-dom-'));
let root;
try {
  const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {MemoryPage} from './src/pages/MemoryPage.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<MemoryPage/>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
  const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
  const {act,mount}=await import(pathToFileURL(path));
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const entries=()=>[...document.querySelectorAll('.scene-entry')];
  const titles=()=>entries().map(node=>node.querySelector('strong').textContent);
  const button=(text,within=document)=>[...within.querySelectorAll('button')].find(node=>node.textContent.trim()===text||node.getAttribute('aria-label')===text);
  async function settle(action,ms=50){await act(async()=>{action?.();await wait(ms);});}
  async function click(node){assert(node,'Element exists');assert(!node.disabled,'Element is enabled');await settle(()=>node.click());}
  async function value(node,text){assert(node,'Input exists');await settle(()=>{
    Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node),'value').set.call(node,text);
    node.dispatchEvent(new Event('input',{bubbles:true}));
  });}

  await settle(()=>{root=mount();});
  assert.equal(entries().length,5,'Old cached records remain visible while new projection loads');
  assert.equal(titles()[0],'Feeling after midnight','Cached records sort by known date immediately');
  assert.equal(titles().at(-1),'Unknown original time','Unknown cached date remains last');
  assert.equal(calls.filter(row=>row.path==='/__serein/live/memory-scenes').length,1,
    'A fresh old-schema cache still refreshes complete time metadata');

  await settle(releaseProjection);
  assert.deepEqual(titles(),['Feeling after midnight','Match late','Match early','Prior dated memory','Unknown original time']);
  assert.match(entries()[0].querySelector('time').textContent,/2026-11-09/);
  assert.match(entries()[0].querySelector('time').textContent,/02:30/,'Feel time uses the Shanghai calendar day');
  assert.match(entries().at(-1).querySelector('time').textContent,/未记录/);
  assert.equal(entries()[3].querySelector('time').textContent,'2026-11-07','An explicit date stays date-only');

  await value(document.querySelector('input[type="search"]'),'Match');
  assert.deepEqual(titles(),['Match late','Match early'],'Filtered results retain full time order');
  await click(button('清空搜索'));

  const prior=entries().find(node=>node.querySelector('strong').textContent==='Prior dated memory');
  await click(prior.querySelector('button'));
  await click(button('编辑记忆',document.querySelector('.scene-detail')));
  await value(document.querySelector('input[aria-label="记忆日期"]'),'2026-11-10');
  await click(button('保存',document.querySelector('.scene-detail')));
  assert.equal(calls.filter(row=>row.path==='/__serein/memory/edit-scene').length,1);
  assert.equal(titles()[0],'Prior dated memory','Editing the declared date immediately reorders the list');
  assert.equal(entries()[0].querySelector('time').textContent,'2026-11-10');

  await click(button('事件',document.querySelector('.memory-type-switch')));
  assert.deepEqual(titles(),['Latest end','Recent start'],'Spanning Events remain sorted by latest end');
  assert.match(document.querySelector('.memory-stream__header').textContent,/结束时间.*(?:倒序|新到旧)/);
  console.log('PASS Memory order DOM: old cache refresh, same-day time order, feel midnight, missing dates, filtering, edited dates and Event end order');
  await settle(()=>root.unmount());root=null;
} finally {
  if(root)root.unmount();
  dom.window.close();
  assert.equal(dirname(resolve(directory)),resolve(tmpdir()));
  assert(basename(directory).startsWith('serein-memory-order-dom-'));
  await rm(directory,{recursive:true,force:true});
}
process.exit(0);
