import test from 'node:test';
import assert from 'node:assert/strict';
import {importProgressSummary} from '../src/utils/importProgress.js';

test('paused, completed and interrupted imports never show an active countdown', () => {
  for (const status of ['queued','paused','completed','failed','interrupted']) {
    assert.equal(importProgressSummary({status,entries_per_second:20,remaining:400,eta_seconds:20}), '');
  }
});

test('early samples show speed without inventing an ETA', () => {
  const summary=importProgressSummary({status:'running',entries_per_second:12.5,remaining:250,eta_seconds:null});
  assert.match(summary,/12.5 条\/秒.*剩余 250 条.*正在估算/);
  assert.doesNotMatch(summary,/预计还需/);
});

test('ETA stays approximate and supports slow imports and hours', () => {
  const summary=importProgressSummary({status:'running',entries_per_second:.25,remaining:1000,eta_seconds:4000});
  assert.match(summary,/0.25 条\/秒.*1 小时 7 分钟.*按近期速度估算/);
  assert.match(importProgressSummary({status:'running',entries_per_second:20,eta_seconds:20}),/1 分钟/);
});

test('invalid or absent measurements do not produce misleading progress', () => {
  for (const entries_per_second of [undefined,null,0,-1,NaN,Infinity]) {
    assert.equal(importProgressSummary({status:'running',entries_per_second}), '');
  }
  assert.doesNotMatch(importProgressSummary({status:'running',entries_per_second:2,remaining:null,eta_seconds:-1}),/剩余 null|预计还需/);
});
