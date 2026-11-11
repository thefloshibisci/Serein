import assert from 'node:assert/strict';
import {test,after} from 'node:test';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
const webRoot=resolve(process.cwd()),require=createRequire(join(webRoot,'package.json'));
const {build,stop}=require('esbuild'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'https://synthetic.invalid'});
for(const key of ['window','document','navigator','HTMLElement','Event','Node','AbortController'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
// jsdom has no top layer; record the modal request and mirror native close.
const proto=dom.window.HTMLDialogElement.prototype;let modal=0;
proto.showModal=function(){modal++;this.setAttribute('open','');};
proto.close=function(){this.removeAttribute('open');this.dispatchEvent(new dom.window.Event('close'));};
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const channels=[],NativeChannel=globalThis.MessageChannel;
globalThis.MessageChannel=class extends NativeChannel{constructor(){super();channels.push(this);}};
const directory=await mkdtemp(join(tmpdir(),'serein-archive-dom-'));
const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {OriginalArchive} from './src/components/OriginalArchive.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<OriginalArchive/>);return root;}`,resolveDir:webRoot,loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"development"'}});
const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
const {act,mount}=await import(pathToFileURL(path));const requests=[];let blocked=false,root,deleteResponse;
const rows=Array.from({length:20},(_,i)=>({id:'raw:'+(i+1),preview:'Synthetic line '+i,truncated:false,metadata:{role:'user',created_at:'2025-01-01',source:'synthetic',session_id:'s'}}));
globalThis.fetch=async(url,options)=>{
  const body=JSON.parse(options.body);requests.push({url,body});let result;
  if(url.endsWith('/search'))result={items:rows,next_before_id:''};
  else if(url.endsWith('/delete-preview'))result={items:[{id:body.ids[0],sha256:'abc',blockers:blocked?['Synthetic blocker']:[]}],scope:'Synthetic scope text',can_delete:!blocked};
  else if(url.endsWith('/delete')){if(deleteResponse)return deleteResponse;result={count:1};}
  else throw new Error('Unexpected request '+url);
  return {ok:true,json:async()=>result};
};
const buttons=text=>[...document.querySelectorAll('button')].filter(node=>node.textContent===text);
const dialog=()=>document.querySelector('dialog.original-archive__confirm');
async function render(){if(root)await act(async()=>root.unmount());requests.length=0;blocked=false;deleteResponse=null;await act(async()=>{root=mount();});await act(async()=>{});}
after(async()=>{if(root)await act(async()=>root.unmount());dom.window.close();stop();for(const c of channels){c.port1.close();c.port2.close();}globalThis.MessageChannel=NativeChannel;await rm(directory,{recursive:true,force:true});});

test('delete confirmation opens as a modal instead of a section below the list',async()=>{
  await render();const before=modal;
  assert.equal(dialog().hasAttribute('open'),false);assert.equal(dialog().textContent,'');
  await act(async()=>{buttons('删除…')[19].click();});
  assert.equal(modal,before+1);assert.equal(dialog().hasAttribute('open'),true);
  assert.match(dialog().textContent,/删除 raw:20？/);assert.match(dialog().textContent,/Synthetic scope text/);
  assert.equal(dialog().getAttribute('aria-labelledby'),dialog().querySelector('h3').id);
  assert.equal(document.querySelector('section.original-archive__confirm'),null);
});
test('cancel and Escape close the modal without deleting',async()=>{
  await render();await act(async()=>{buttons('删除…')[0].click();});
  await act(async()=>{buttons('取消')[0].click();});
  assert.equal(dialog().hasAttribute('open'),false);
  await act(async()=>{buttons('删除…')[0].click();});assert.equal(dialog().hasAttribute('open'),true);
  await act(async()=>{dialog().close();});
  assert.equal(dialog().textContent,'');assert(!requests.some(r=>r.url.endsWith('/delete')));
});
test('a blocked original cannot be confirmed from the modal',async()=>{
  await render();blocked=true;await act(async()=>{buttons('删除…')[2].click();});
  assert.match(dialog().textContent,/Synthetic blocker/);assert.equal(buttons('确认删除原话')[0].disabled,true);
});
test('confirming deletes exactly the previewed original and closes the modal',async()=>{
  await render();await act(async()=>{buttons('删除…')[4].click();});
  await act(async()=>{buttons('确认删除原话')[0].click();});
  assert.deepEqual(requests.find(r=>r.url.endsWith('/delete')).body,{items:[{id:'raw:5',sha256:'abc'}],confirm:true});
  assert.equal(dialog().hasAttribute('open'),false);assert.match(document.body.textContent,/已删除 1 条档案原话/);
});

test('the modal remains open while deleting and reports failures beside the confirmation',async()=>{
  await render();await act(async()=>{buttons('删除…')[4].click();});
  let finish;deleteResponse=new Promise(resolve=>{finish=resolve;});
  await act(async()=>{buttons('确认删除原话')[0].click();});
  assert.equal(buttons('取消')[0].disabled,true);
  const cancel=new dom.window.Event('cancel',{cancelable:true});
  await act(async()=>{dialog().dispatchEvent(cancel);});
  assert.equal(cancel.defaultPrevented,true);assert.equal(dialog().open,true);
  await act(async()=>{finish({ok:false,json:async()=>({detail:'Synthetic stale original'})});});
  assert.equal(dialog().open,true);assert.equal(buttons('取消')[0].disabled,false);
  assert.equal(dialog().querySelector('[role="alert"]').textContent,'Synthetic stale original');
  assert.equal(document.querySelectorAll('[role="alert"]').length,1);
  await act(async()=>{buttons('取消')[0].click();});
  assert.equal(document.activeElement,buttons('删除…')[4]);
});
