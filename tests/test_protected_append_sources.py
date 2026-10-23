"""Protected retries need genuinely new evidence, never another card's sources."""
import asyncio
import copy
import json

import pytest

from serein.extensions import pipeline as p
from serein.extensions import pipeline_latest as latest


def case(stable=(1, 2, 3, 4), *, manual=False, action='extend', opt_in=True):
    messages = [{'id': i, 'session_id': 1, 'role': 'user' if i % 2 else 'assistant',
                 'content': f'Synthetic message {i}', 'created_at': '2026-09-01T00:00:00Z', 'metadata': {}}
                for i in range(1, 7)]
    base = {'event_id': 'target', 'primary_track_id': 'a', 'session_ids': [1],
            'source_message_ids': [1, 2], 'predecessor_event_ids': [], 'active': True,
            'protected': True, 'continuation_allowed': True, 'title': 'Frozen title',
            'body': 'Frozen original body', 'recallable': False}
    bases = [base]
    if manual:
        bases.append({**base, 'event_id': 'manual-other', 'source_message_ids': [3, 4],
                      'manual': True, 'continuation_allowed': False, 'body': 'Other original body'})
    component = {'component_id': 'synthetic', 'track_ids': ['a'], 'track_cards': [],
                 'messages': [m for m in messages if m['id'] in stable],
                 'context_messages': messages, 'context_session_ids': [1],
                 'memberships': [{'unit_root_message_id': i, 'track_id': 'a',
                                  'session_id': 1, 'source_message_ids': [i, i + 1],
                                  'routing_role': 'primary_activity'}
                                 for i in (1, 3, 5) if i in stable],
                 'context_edges': [], 'parked_context_source_ids': [],
                 'base_event_candidates': bases, 'append_protected': opt_in}
    proposal = {'events': [{'action': action, 'primary_track_id': 'a',
                           'base_event_ids': ['target', 'manual-other'] if action == 'merge' else ['target'],
                           'owned_unit_roots': [i for i in (1, 3, 5) if i in stable]}],
                'skip_unit_roots': [], 'defer_unit_roots': [],
                'decision_review': {'events': [{'event_index': 0, 'reason': 'Synthetic continuation'}],
                                    'boundaries': [], 'dispositions': []}}
    return component, proposal


def test_retry_keeps_old_union_and_accepts_only_new_evidence():
    component, proposal = case()
    before = copy.deepcopy(component)
    plan = latest.normalize_event_curator_output(proposal, component)
    assert plan['events'][0]['append_only'] is True
    assert plan['events'][0]['source_message_ids'] == [1, 2, 3, 4]
    assert component == before


@pytest.mark.parametrize('kind', ['all_old', 'disabled', 'forked', 'unselected_manual'])
def test_append_cannot_bypass_protection(kind):
    component, proposal = case(stable=(1, 2) if kind == 'all_old' else (1, 2, 3, 4),
                               opt_in=kind != 'disabled', manual=kind == 'unselected_manual')
    if kind == 'forked':
        component['base_event_candidates'][0]['forked'] = True
    plan = latest.normalize_event_curator_output(proposal, component)
    assert plan['events'] == []
    assert plan['defer_source_message_ids'] == [m['id'] for m in component['messages']]


@pytest.mark.parametrize('stable', [(1, 2, 3, 4), (1, 2, 3, 4, 5, 6)])
def test_merge_with_manual_card_cannot_turn_its_old_sources_into_new_evidence(stable):
    component, proposal = case(stable, manual=True, action='merge')
    plan = latest.normalize_event_curator_output(proposal, component)
    assert plan['events'] == []
    assert plan['defer_source_message_ids'] == list(stable)
    assert set(plan['hard_skips'][0]['active_base_event_ids']) == {'target', 'manual-other'}


@pytest.mark.parametrize('concurrency', [1, 2])
def test_writer_gets_only_new_sources_and_old_body_as_context(monkeypatch, concurrency):
    component, proposal = case()
    event = latest.normalize_event_curator_output(proposal, component)['events'][0]
    monkeypatch.setattr(p, 'snapshot', lambda *args: {
        'identity': {'user_name': 'User', 'ai_name': 'AI'}, 'models': {}, 'revision': 1,
        'policy': {'execution_mode': 'agent'}})
    monkeypatch.setattr(p, 'event_writer_concurrency', lambda *args: concurrency)
    captured = []
    async def job(database, batch, request, key, runner):
        captured.append(request)
        return {'title': 'New title', 'event_draft': 'New progress', 'evidence_sufficient': True}
    monkeypatch.setattr(p, 'job', job)
    # Two frozen requests exercise the parallel path as well as the serial path.
    events = [event, {**event, 'event_ref': 'another-request'}]
    batch = {'id': 'synthetic', 'input_json': json.dumps({'day': '2026-09-01'})}
    result = asyncio.run(p.first_event_writer_pass(None, batch, component,
                        {'events': events}, 0, None))
    assert len(result) == len(captured) == 2
    for request in captured:
        assert [m['id'] for m in request['messages']] == [3, 4]
        assert 'Frozen original body' in request['prompt']
        assert '旧正文由程序原样保留并追加' in request['prompt']
    assert component['base_event_candidates'][0]['body'] == 'Frozen original body'
