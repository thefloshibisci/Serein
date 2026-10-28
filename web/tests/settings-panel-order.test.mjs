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
