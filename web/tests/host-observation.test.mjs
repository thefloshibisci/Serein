import test from 'node:test';
import assert from 'node:assert/strict';
import {hostObservationDetails, resolveBridgeObservationOutcome} from '../src/recallObservationOutcome.js';

test('host diagnostics show time, outcome and prepared details without inventing delivery', () => {
  const row = {observation_version:1, recall_status:'ready', request_status:'interrupted', elapsed_ms:123,
    gateway_memory_injected_ids:[], prepared_items:[{id:'scene:test',title:'Synthetic title',score:.91,source_kind:'scene'}]};
  const view = hostObservationDetails(row);
  assert.match(view.requestLabel, /123ms/);
  assert.match(view.requestLabel, /回复已中断/);
  assert.equal(view.prepared[0].title, 'Synthetic title');
  assert.equal(resolveBridgeObservationOutcome({hookOutcome:'failed',injected:[{id:'scene:test'}]}),'failed');
  assert.equal(resolveBridgeObservationOutcome({hookOutcome:'no_match'}),'no_match');
  assert.deepEqual(hostObservationDetails({}),{});
});
test('successful receipt means completed delivery, not proof of model use', () => {
  const view = hostObservationDetails({observation_version:1,recall_status:'ready',request_status:'completed',gateway_memory_injected_ids:['scene:test']});
  assert.match(view.requestLabel, /APP 确认交付/);
  assert.match(view.requestLabel, /不代表模型使用/);
});
