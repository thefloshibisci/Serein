import test from 'node:test';
import assert from 'node:assert/strict';
import {appendResumePage,resumeMaterials,resumePagesText} from '../src/utils/resumePages.js';

const first={collection_id:'collection-a',items:[{id:'event:a',kind:'event',section:'recent_event',title:'Synthetic event',created_at:'2025-01-02T00:00:00Z',body_md:'first ',body_offset:0,body_complete:false}],
  total_items:1,has_more:true,next_cursor:'collection-a:0:6',handoff:null};
const second={...first,items:[{...first.items[0],body_md:'second',body_offset:6,body_complete:true}],has_more:false,next_cursor:null};

test('a long body is complete only after all matching pages are read',()=>{
  let pages=appendResumePage([],first);
  assert.throws(()=>resumePagesText(pages),/全部页面/);
  pages=appendResumePage(pages,second,first.next_cursor);
  assert.equal(resumeMaterials(pages)[0].body_md,'first second');
  assert.match(resumePagesText(pages),/first second/);
  assert.match(resumePagesText(pages),/created_at: 2025-01-02T00:00:00Z/);
  assert.equal(first.items[0].body_md,'first ');
});

test('changed collections, duplicate pages and missing fragments cannot be copied as complete',()=>{
  assert.throws(()=>appendResumePage([first],{...second,collection_id:'changed'},first.next_cursor),/已变化/);
  assert.throws(()=>appendResumePage([first],{...second,items:[{...second.items[0],body_offset:5}]},first.next_cursor),/不连续/);
  assert.throws(()=>appendResumePage([],second),/缺少前一页/);
  const pages=appendResumePage([first],second,first.next_cursor);
  assert.throws(()=>appendResumePage(pages,second,first.next_cursor),/已变化/);
});

test('emoji fragments use server character offsets and retain the complete original text',()=>{
  const start={...first,items:[{...first.items[0],body_md:'雨🌧️',body_offset:0}]};
  const end={...second,items:[{...second.items[0],body_md:'继续聊。',body_offset:3}]};
  const pages=appendResumePage([start],end,start.next_cursor);
  assert.equal(resumeMaterials(pages)[0].body_md,'雨🌧️继续聊。');
  assert.match(resumePagesText(pages),/雨🌧️继续聊。/);
});
