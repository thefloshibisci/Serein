import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, basename} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {build, stop} from 'esbuild';
import {JSDOM} from 'jsdom';

test('unvisited pages stay idle, visited drafts survive, and slow personal sync does not block observations', {timeout:30000}, async()=>{
  const directory=await mkdtemp(join(tmpdir(),'serein-slow-network-'));
  const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost/#cover',pretendToBeVisual:true});
  const globals=['window','document','navigator','getComputedStyle','requestAnimationFrame','IS_REACT_ACT_ENVIRONMENT','fetch','MessageChannel'];
  const previous=new Map(globals.map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
  let root, act;
  const channels=[];
  const NativeMessageChannel=globalThis.MessageChannel;
  try {
    for(const [key,value] of Object.entries({window:dom.window,document:dom.window.document,navigator:dom.window.navigator,
      getComputedStyle:dom.window.getComputedStyle,requestAnimationFrame:callback=>setTimeout(callback,0),IS_REACT_ACT_ENVIRONMENT:true})) {
      Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});
    }
    globalThis.MessageChannel=class extends NativeMessageChannel {constructor(){super();channels.push(this);}};
    window.matchMedia=()=>({matches:true}); window.scrollTo=()=>{}; window.mounts={};
    const output=await build({stdin:{contents:`
      import React,{act} from 'react';import {createRoot} from 'react-dom/client';
      import {App} from './src/App.jsx';
      import {BasementRecallObservation} from './src/components/BasementRecallObservation.jsx';
      export {act,createRoot};export const app=<App/>;export const observations=<BasementRecallObservation/>;
    `,resolveDir:fileURLToPath(new URL('..',import.meta.url)),loader:'jsx'},bundle:true,platform:'node',format:'esm',write:false,jsx:'automatic',
      loader:{'.css':'empty'},banner:{js:'import {createRequire} from "node:module";const require=createRequire(import.meta.url);'},
      define:{'process.env.NODE_ENV':'"test"','import.meta.env.BASE_URL':'"/"'},plugins:[{name:'page-fixtures',setup(builder){
        builder.onLoad({filter:/[/\\]pages[/\\][^/\\]+\.jsx$/},args=>{
          const name=basename(args.path,'.jsx');
          if(name==='AwakePage') return {loader:'jsx',contents:`export function AwakePage({onNavigate}) {return <nav>{['醒来','日记','叙事卷','地下室'].map(area=><button key={area} data-go={area} onClick={()=>onNavigate(area)}>{area}</button>)}</nav>;}`};
          const names=name==='CompanionPages'?['PersonaPage','MemosPage']:[name];
          return {loader:'jsx',contents:`import {useEffect,useState} from 'react';${names.map(value=>`export function ${value}(){const [count,setCount]=useState(0);useEffect(()=>{window.mounts['${value}']=(window.mounts['${value}']||0)+1;},[]);return <button data-page="${value}" onClick={()=>setCount(count+1)}>{count}</button>;}`).join('\n')}`};
        });
      }}]});
    const path=join(directory,'bundle.mjs'); await writeFile(path,output.outputFiles[0].text);
    const ui=await import(pathToFileURL(path)); act=ui.act;
    root=ui.createRoot(document.getElementById('root'));
    await act(async()=>root.render(ui.app));
    assert.deepEqual(window.mounts,{});
    const click=async selector=>act(async()=>document.querySelector(selector).dispatchEvent(new window.MouseEvent('click',{bubbles:true})));
    await click('[data-go="日记"]'); await act(async()=>{});
    assert.equal(window.mounts.DiaryPage,1);
    assert.equal(window.mounts.BasementPage,undefined);
    await click('[data-page="DiaryPage"]');
    await click('[data-go="醒来"]'); await click('[data-go="日记"]');
    assert.equal(document.querySelector('[data-page="DiaryPage"]').textContent,'1');
    assert.equal(window.mounts.DiaryPage,1);
    await act(async()=>root.unmount());

    const calls=[]; let releaseGateway;
    globalThis.fetch=async (url,options={})=>{
      calls.push(String(url));
      if(String(url).startsWith('/__serein/personal')) return new Promise((_,reject)=>{
        options.signal?.addEventListener('abort',()=>reject(new DOMException('timeout','AbortError')),{once:true});
      });
      const payload=String(url).endsWith('/injections') || String(url).endsWith('/hook-injections')
        ? {status:'ok',items:[{id:1,payload:{observation_version:1,request_status:'completed',request_kind:'user_turn',query:'NETWORK_TEST_RECORD',hook_memory_outcome:'no_match'}}],has_more:false}
        : {status:'ok',routes:[],dataset_version:1};
      const response={ok:true,status:200,json:async()=>payload};
      if(String(url)==='/__serein/gateway/injections') return new Promise(resolve=>{releaseGateway=()=>resolve(response);});
      return response;
    };
    root=ui.createRoot(document.getElementById('root'));
    await act(async()=>root.render(ui.observations));
    await act(async()=>{});
    assert.ok(calls.some(url=>url.startsWith('/__serein/personal')));
    assert.ok(calls.includes('/__serein/gateway/injections'));
    assert.ok(document.querySelector('.observation-loading'));
    assert.equal(document.querySelector('.basement-empty-state'),null);
    await act(async()=>releaseGateway());
    assert.ok(document.body.textContent.includes('NETWORK_TEST_RECORD'));
  } finally {
    if(root && act) await act(async()=>root.unmount());
    for(const channel of channels){channel.port1.close();channel.port2.close();}
    dom.window.close();stop();await rm(directory,{recursive:true,force:true});
    for(const [key,descriptor] of previous) {if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}
  }
});
