import test from 'node:test';
import assert from 'node:assert/strict';
import {appendResumePage,resumeDate,resumeMaterials,resumePagesText} from '../src/utils/resumePages.js';

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

test('source date is shown separately from storage time, including copied context',()=>{
  const item={...second.items[0],body_offset:0,date:'2026-08-30',date_basis:'source_records',
    reference_time:'2026-08-30T10:29:20+08:00',created_at:'2026-11-08T03:59:38Z'};
  assert.deepEqual(resumeDate(item),{value:'2026-08-30',label:'来源日期'});
  const copied=resumePagesText([{...second,items:[item]}]);
  assert.match(copied,/created_at: 2026-11-08/);
  assert.match(copied,/date: 2026-08-30/);
  assert.match(copied,/date_basis: source_records/);
  assert.deepEqual(resumeDate({kind:'event',created_at:item.created_at}),
    {value:'2026-11-08',label:'入库日期（来源时间未知）'});
  assert.deepEqual(resumeDate({kind:'event',date_basis:'unknown'}),{value:'',label:'日期未知'});
  assert.deepEqual(resumeDate({kind:'scene',created_at:'2025-01-02T00:00:00Z'}),{value:'2025-01-02',label:''});
});
