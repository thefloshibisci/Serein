import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {build,stop} from 'esbuild';
import {JSDOM} from 'jsdom';

test('settings tabs map to eight slides with one active page',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'serein-settings-order-'));
  let dom;
  try {
    const output=await build({stdin:{contents:`
      import React from 'react';
      import {renderToStaticMarkup} from 'react-dom/server';
      import {SettingsPanel} from './src/components/SettingsPanel.jsx';
      import {defaultCoverSettings} from './src/data/awake.js';
      export const html=renderToStaticMarkup(<SettingsPanel open coverSettings={defaultCoverSettings}
        identityNames={{user:'User',assistant:'AI'}} images={{}}
        anniversary={{today:'2026-01-01',draft:'2026-01-01'}}/>);
    `,resolveDir:fileURLToPath(new URL('..',import.meta.url)),loader:'jsx'},jsx:'automatic',bundle:true,
      platform:'node',format:'esm',write:false,loader:{'.css':'empty'},
      banner:{js:'import {createRequire} from "node:module";const require=createRequire(import.meta.url);'},
      define:{'process.env.NODE_ENV':'"production"','import.meta.env.BASE_URL':'"/"'}});
    const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
    const {html}=await import(pathToFileURL(path));
    dom=new JSDOM(html);
    const tabs=[...dom.window.document.querySelectorAll('[role="tab"]')];
    const slides=[...dom.window.document.querySelectorAll('.settings-panel__body > [role="tabpanel"]')];
    assert.equal(slides.length,8);
    assert.equal(slides.filter(panel=>panel.getAttribute('aria-hidden')==='false').length,1);
    assert.deepEqual(tabs.map(tab=>tab.getAttribute('aria-controls')),slides.map(slide=>slide.id));
    assert(tabs.some(tab=>tab.textContent==='自动摘要管理'));
    assert(tabs.some(tab=>tab.textContent==='原文档案'));
  } finally {
    dom?.window.close();stop();await rm(directory,{recursive:true,force:true});
  }
});

test('settings targets actual slides and loads only visited pages',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'serein-settings-resize-'));
  const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
  const globals=['window','document','navigator','HTMLElement','Event','CustomEvent','Node','ResizeObserver','fetch','IS_REACT_ACT_ENVIRONMENT'];
  const saved=new Map(globals.map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
  const channels=[],NativeChannel=globalThis.MessageChannel;
  globalThis.MessageChannel=class extends NativeChannel{constructor(){super();channels.push(this);}};
  let root,act,width=332.4;const requests=[],scrolls=[];
  try {
    for(const key of globals.slice(0,7))Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
    Object.defineProperty(globalThis,'IS_REACT_ACT_ENVIRONMENT',{value:true,configurable:true});
    Object.defineProperty(globalThis,'fetch',{value:url=>{requests.push(url);return new Promise(()=>{});},configurable:true});
    dom.window.matchMedia=()=>({matches:false});
    dom.window.HTMLElement.prototype.scrollTo=function(options){scrolls.push(options);this.scrollLeft=options.left;};
    // DOM tests verify targets/semantics only. Native browser checks cover the
    // intermediate animation, snap, CSS sizing and viewport-change behavior.
    Object.defineProperty(dom.window.HTMLElement.prototype,'offsetLeft',{configurable:true,get(){
      return [...document.querySelectorAll('.settings-panel__body > [role=tabpanel]')].indexOf(this)*width;
    }});
    const output=await build({stdin:{contents:`
      import React,{act} from 'react';import {createRoot} from 'react-dom/client';
      import {SettingsPanel} from './src/components/SettingsPanel.jsx';
      import {defaultCoverSettings} from './src/data/awake.js';export {act};
      export function mount(){const root=createRoot(document.getElementById('root'));root.render(
        <SettingsPanel open coverSettings={defaultCoverSettings} identityNames={{user:'User',assistant:'AI'}}
          images={{}} anniversary={{today:'2026-01-01',draft:'2026-01-01'}}/>);return root;}
    `,resolveDir:fileURLToPath(new URL('..',import.meta.url)),loader:'jsx'},jsx:'automatic',bundle:true,
      platform:'node',format:'esm',write:false,loader:{'.css':'empty'},
      define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
    const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
    const fixture=await import(pathToFileURL(path));act=fixture.act;
    await act(async()=>{root=fixture.mount();});
    assert.deepEqual(requests,[],'Opening appearance does not start hidden configuration or import requests');
    const selected=key=>{
      const panels=[...document.querySelectorAll('.settings-panel__body > [role=tabpanel]')];
      assert.equal(panels.length,8);
      assert.deepEqual(panels.filter(panel=>panel.getAttribute('aria-hidden')==='false').map(panel=>panel.id),['settings-content-'+key]);
      assert.equal(document.querySelector('[role=tab][aria-selected=true]').id,'settings-tab-'+key);
      for(const panel of panels)assert.equal(panel.hidden,false,'All panels retain layout slots for sliding');
    };
    const pages=document.querySelector('.settings-panel__body');
    await act(async()=>{document.getElementById('settings-tab-summary').click();});
    assert.equal(scrolls.at(-1).behavior,'smooth','Native sliding remains enabled');
    assert.equal(pages.scrollLeft,4*width);
    width=254.6;
    await act(async()=>{pages.dispatchEvent(new window.Event('scrollend',{bubbles:true}));});
    assert.equal(pages.scrollLeft,4*width,'A resize during sliding settles at the current panel position');
    for(const key of ['summary','appearance','originals','models','configuration','features','imports','migration','summary']){
      await act(async()=>{document.getElementById('settings-tab-'+key).click();});
      selected(key);
      const index=[...document.querySelectorAll('[role=tab]')].findIndex(item=>item.id==='settings-tab-'+key);
      assert.equal(pages.scrollLeft,index*width);
    }
    const modelPage=document.getElementById('settings-content-models');
    const importPage=document.getElementById('settings-content-imports');
    await act(async()=>{
      document.getElementById('settings-tab-appearance').click();
      document.getElementById('settings-tab-imports').click();
      document.getElementById('settings-tab-summary').click();
    });
    selected('summary');
    assert.equal(pages.scrollLeft,4*width);
    assert.equal(document.getElementById('settings-content-models'),modelPage,'Shared model editor remains mounted');
    assert.equal(document.getElementById('settings-content-imports'),importPage,'Visited import editor remains mounted');
    await act(async()=>{document.getElementById('settings-tab-summary').dispatchEvent(new window.KeyboardEvent('keydown',{key:'End',bubbles:true}));});
    selected('migration');assert.equal(document.activeElement.id,'settings-tab-migration');
    await act(async()=>{document.activeElement.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Home',bubbles:true}));});
    selected('appearance');assert.equal(document.activeElement.id,'settings-tab-appearance');
    await act(async()=>{window.dispatchEvent(new window.CustomEvent('serein:open-settings-tab',{detail:'summary'}));});
    selected('summary');
    dom.window.matchMedia=()=>({matches:true});
    await act(async()=>{document.getElementById('settings-tab-originals').click();});
    assert.equal(pages.scrollLeft,5*width,'Reduced motion settles immediately');
    await act(async()=>{pages.dispatchEvent(new window.Event('pointerdown',{bubbles:true}));pages.scrollLeft=width;pages.dispatchEvent(new window.Event('scroll',{bubbles:true}));});
    selected('features');
    assert(scrolls.some(item=>item.behavior==='instant'),'Reduced motion uses immediate alignment');
    assert.equal(requests.filter(url=>url==='/__serein/settings').length,3,'Each visited data editor loads once, not once per switch');
  } finally {
    if(root)await act(async()=>root.unmount());dom.window.close();stop();
    for(const channel of channels){channel.port1.close();channel.port2.close();}globalThis.MessageChannel=NativeChannel;
    for(const [key,descriptor]of saved)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
    await rm(directory,{recursive:true,force:true});
  }
});
