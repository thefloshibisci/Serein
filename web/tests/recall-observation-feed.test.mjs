import test from 'node:test';
import assert from 'node:assert/strict';
import { createObservationFeed, pendingObservationIds } from '../src/storage/recallObservationFeed.js';
import { mergeObservationRows, recallObservationPageLimits } from '../src/storage/recallObservationPagination.js';
const row = (id, status = 'completed', revision = 1) => ({ id, payload: { observation_version: 1, request_kind: 'user_turn', request_status: status, observation_revision: revision } });
const page = (items, extra = {}) => ({ status: 'ok', items, has_more: false, next_before_id: items.at(-1)?.id || 0, ...extra });
function setup(pages, options = {}) {
  const calls = [];
  const feed = createObservationFeed({ request: async (source, args) => { calls.push({ source, ...args }); return pages.shift(); }, onChange() {}, ...options });
  return { feed, calls };
}

test('initial windows are 20; reviewed older rows do not inflate the visible window', async () => {
  assert.deepEqual(recallObservationPageLimits, { hook: 20, gateway: 20 });
  const { feed } = setup([page(Array.from({ length: 20 }, (_, n) => row(40 - n)), { reviewed_items: [row(1)], has_more: true })]);
  await feed.refresh('gateway');
  assert.equal(feed.snapshot().gateway.rows.length, 20);
  assert.equal(feed.snapshot().gateway.reviewed[0].id, 1);
  feed.dispose();
});

test('refresh rechecks old pending rows and buffers new rows without losing older pagination', async () => {
  const { feed, calls } = setup([
    page([row(30), row(29, 'upstream_pending')], { has_more: true, next_before_id: 29 }),
    page([row(31)], { reviewed_items: [row(29, 'completed', 3)], next_after_id: 31 }),
    page([row(28), row(27)], { has_more: true, next_before_id: 27 }),
  ]);
  await feed.refresh('gateway');
  await feed.refresh('gateway');
  let current = feed.snapshot().gateway;
  assert.equal(calls[1].afterId, 30);
  assert.deepEqual(calls[1].reviewIds, [29]);
  assert.deepEqual(current.rows.map(r => r.id), [30, 29]);
  assert.equal(current.rows[1].payload.request_status, 'completed');
  assert.deepEqual(current.buffered.map(r => r.id), [31]);
  assert.equal(current.nextBeforeId, 29);
  assert.equal(current.hasMore, true);
  await feed.loadEarlier('gateway');
  assert.equal(calls[2].beforeId, 29);
  feed.reveal('gateway');
  current = feed.snapshot().gateway;
  assert.deepEqual(current.rows.map(r => r.id), [31, 30, 29, 28, 27]);
  assert.equal(current.afterId, 31);
  assert.equal(current.nextBeforeId, 27);
  feed.dispose();
});

test('ascending delta cursor catches more than one page of new rows without gaps', async () => {
  const { feed, calls } = setup([
    page([row(10)], { next_before_id: 10 }),
    page(Array.from({ length: 20 }, (_, n) => row(n + 11)), { has_more: true, next_after_id: 30 }),
    page(Array.from({ length: 5 }, (_, n) => row(n + 31)), { next_after_id: 35 }),
  ], { canReveal: () => true });
  for (let n = 0; n < 3; n++) await feed.refresh('gateway');
  assert.equal(calls[2].afterId, 30);
  assert.deepEqual(feed.snapshot().gateway.rows.map(r => r.id), Array.from({ length: 26 }, (_, n) => 35 - n));
  feed.dispose();
});

test('buffered pending records are updated and terminal states never regress', async () => {
  const { feed } = setup([page([]), page([row(1, 'preparing')], { next_after_id: 1 }),
    page([], { reviewed_items: [row(1, 'interrupted', 3)], next_after_id: 1 })]);
  await feed.refresh('gateway');
  await feed.refresh('gateway');
  assert.deepEqual(pendingObservationIds(feed.snapshot().gateway), [1]);
  await feed.refresh('gateway');
  feed.reveal('gateway');
  const rows = feed.snapshot().gateway.rows;
  assert.equal(rows[0].payload.request_status, 'interrupted');
  assert.deepEqual(mergeObservationRows(rows, [row(1, 'upstream_pending', 2)]), rows);
  feed.dispose();
});

test('concurrent refresh and history reads are serialized; disposal aborts and prevents late writes', async () => {
  let resolve;
  let signal;
  let changes = 0;
  let requests = 0;
  const feed = createObservationFeed({ request: (_, opts) => {
    requests++; signal = opts.signal; return new Promise(done => { resolve = done; });
  }, onChange: () => { changes++; } });
  const first = feed.refresh('gateway');
  const second = feed.refresh('gateway');
  const third = feed.loadEarlier('gateway');
  assert.equal(requests, 1);
  feed.dispose();
  assert.equal(signal.aborted, true);
  const previous = changes;
  resolve(page([row(1)]));
  await Promise.all([first, second, third]);
  assert.equal(changes, previous);
});

test('failure keeps the visible rows and both cursors for a safe retry', async () => {
  let count = 0;
  const feed = createObservationFeed({ onChange() {}, request: async () => {
    if (count++ === 0) return page([row(5)], { has_more: true });
    throw new Error('temporary error');
  } });
  await feed.refresh('hook');
  await feed.refresh('hook');
  const value = feed.snapshot().hook;
  assert.deepEqual(value.rows.map(r => r.id), [5]);
  assert.equal(value.afterId, 5);
  assert.equal(value.nextBeforeId, 5);
  assert.equal(value.error, 'temporary error');
  assert.equal(value.loading, false);
  feed.dispose();
});

test('late personal reviews recover older evidence without replacing the live window or cursor', async () => {
  let reviews = {};
  const {feed, calls} = setup([page([row(30)], {has_more:true}),
    page([row(31)], {reviewed_items:[row(2)], next_after_id:31}), page([])], {reviews:()=>reviews});
  await feed.refresh('hook');
  reviews = {'hook-2':{verdict:'useful'}};
  await feed.refreshReviewed('hook');
  assert.deepEqual(calls[1].reviewIds, [2]);
  assert.deepEqual(feed.snapshot().hook.rows.map(r=>r.id), [30]);
  assert.deepEqual(feed.snapshot().hook.buffered.map(r=>r.id), [31]);
  assert.deepEqual(feed.snapshot().hook.reviewed.map(r=>r.id), [2]);
  assert.equal(feed.snapshot().hook.nextBeforeId, 30);
  assert.equal(feed.snapshot().hook.afterId, 31);
  await feed.refresh('hook');
  assert.deepEqual(calls[2].reviewIds, []);
  feed.dispose();
});

test('review hydration during an in-flight initial page is reconciled afterwards', async () => {
  let reviews = {}, resolve;
  const calls = [];
  const feed = createObservationFeed({onChange(){},reviews:()=>reviews,request:async (_, options)=>{
    calls.push(options);
    return calls.length === 1 ? new Promise(done=>{resolve=done;}) : page([], {reviewed_items:[row(1)]});
  }});
  const first = feed.refresh('gateway');
  reviews = {'1':{verdict:'useful'}};
  const hydration = feed.refreshReviewed('gateway');
  resolve(page([row(20)]));
  await Promise.all([first, hydration]);
  assert.deepEqual(calls.map(call=>call.reviewIds), [[],[1]]);
  assert.deepEqual(feed.snapshot().gateway.reviewed.map(r=>r.id), [1]);
  feed.dispose();
});

test('a full pending window cannot starve late reviews and resumes pending checks afterwards', async()=>{
  let reviews={};
  const pending=Array.from({length:500},(_,n)=>row(n+10,'upstream_pending'));
  const {feed,calls}=setup([page(pending),page([], {reviewed_items:[row(1)]}),page([])],{reviews:()=>reviews});
  await feed.refresh('hook');reviews={'hook-1':{}};
  await feed.refreshReviewed('hook');
  assert.equal(calls[1].reviewIds.length,500);
  assert.equal(calls[1].reviewIds[0],1);
  await feed.refresh('hook');
  assert.equal(calls[2].reviewIds.length,500);
  assert.equal(calls[2].reviewIds[0],10);
  feed.dispose();
});
