import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {build,stop} from 'esbuild';
import {JSDOM} from 'jsdom';

test('settings tab positions match rendered slides with both summaries and original archives',async()=>{
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
    assert.deepEqual(tabs.map(tab=>tab.getAttribute('aria-controls')),slides.map(slide=>slide.id));
    assert(tabs.some(tab=>tab.textContent==='自动摘要管理'));
    assert(tabs.some(tab=>tab.textContent==='原文档案'));
  } finally {
    dom?.window.close();stop();await rm(directory,{recursive:true,force:true});
  }
});

test('all settings tabs stay aligned through long jumps, rapid switches and viewport changes',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'serein-settings-resize-'));
  const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
  const globals=['window','document','navigator','HTMLElement','Event','CustomEvent','Node','ResizeObserver','fetch','IS_REACT_ACT_ENVIRONMENT'];
  const saved=new Map(globals.map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
  const channels=[],NativeChannel=globalThis.MessageChannel;
  globalThis.MessageChannel=class extends NativeChannel{constructor(){super();channels.push(this);}};
  let root,act,resize,width=372.4;
  try {
    for(const key of globals.slice(0,7))Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
    Object.defineProperty(globalThis,'IS_REACT_ACT_ENVIRONMENT',{value:true,configurable:true});
    Object.defineProperty(globalThis,'ResizeObserver',{value:class {constructor(callback){resize=callback;}observe(){}disconnect(){}},configurable:true});
    // Keep unrelated configuration requests unresolved; this fixture exercises
    // real tab navigation and dimensions without a backend or model calls.
    Object.defineProperty(globalThis,'fetch',{value:()=>new Promise(()=>{}),configurable:true});
    dom.window.matchMedia=()=>({matches:false});
    dom.window.HTMLElement.prototype.scrollTo=function({left=0}){this.scrollLeft=left;};
    dom.window.HTMLElement.prototype.getBoundingClientRect=function(){
      const panels=[...document.querySelectorAll('.settings-panel__body > [role="tabpanel"]')];
      const index=panels.indexOf(this);
      return {left:index<0?58:58+index*width-this.parentElement.scrollLeft,width};
    };
    Object.defineProperty(dom.window.HTMLElement.prototype,'clientWidth',{configurable:true,get:()=>Math.round(width)});
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
    const pages=document.querySelector('.settings-panel__body');
    await act(async()=>{document.getElementById('settings-tab-originals').click();});
    width=332.4;
    await act(async()=>{resize?.();await new Promise(resolve=>setTimeout(resolve,520));});
    assert(Math.abs(pages.scrollLeft-5*width)<0.01,'The final snap uses the current panel position, including fractional widths');
    assert.equal(document.getElementById('settings-tab-originals').getAttribute('aria-selected'),'true');
    assert.equal(document.getElementById('settings-content-originals').getAttribute('aria-hidden'),'false');
    assert.equal(document.getElementById('settings-content-imports').getAttribute('aria-hidden'),'true');
    assert.equal(document.getElementById('settings-content-originals').getBoundingClientRect().left,58);
    width=254;
    await act(async()=>{resize?.();});
    assert.equal(pages.scrollLeft,5*width,'Idle viewport changes keep the current tab aligned');
    // Appearance -> summary crosses several pages, using the same navigation
    // path as originals. Cover every rendered panel, not only the archive.
    for(const key of ['appearance','summary','models','configuration','features','imports','migration','originals']){
      await act(async()=>{document.getElementById('settings-tab-'+key).click();});
      width+=.6;
      await act(async()=>{resize?.();await new Promise(resolve=>setTimeout(resolve,500));});
      assert(Math.abs(document.getElementById('settings-content-'+key).getBoundingClientRect().left-58)<.01,key+' is aligned');
      assert.equal(document.querySelector('[role="tab"][aria-selected="true"]').id,'settings-tab-'+key);
      assert.equal(document.querySelectorAll('[role="tabpanel"][aria-hidden="false"]').length,1);
    }
    await act(async()=>{document.getElementById('settings-tab-appearance').click();});
    await act(async()=>{document.getElementById('settings-tab-imports').click();});
    await act(async()=>{document.getElementById('settings-tab-summary').click();});
    width=332.4;
    await act(async()=>{resize?.();await new Promise(resolve=>setTimeout(resolve,520));});
    assert.equal(pages.scrollLeft,4*width,'Only the latest requested tab wins after quick consecutive changes');
    assert.equal(document.getElementById('settings-tab-summary').getAttribute('aria-selected'),'true');
  } finally {
    if(root)await act(async()=>root.unmount());dom.window.close();stop();
    for(const channel of channels){channel.port1.close();channel.port2.close();}globalThis.MessageChannel=NativeChannel;
    for(const [key,descriptor]of saved)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
    await rm(directory,{recursive:true,force:true});
  }
});
