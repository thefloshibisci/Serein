"""Synthetic declared bridges for bounded joint Event review."""

import copy

import pytest

from serein.extensions.pipeline_continuity import (
    bounded_components, validate_bridge_owners, validate_continuations,
)
from serein.extensions.pipeline_latest import normalize_event_curator_output


def _component(track, messages, bridge=None):
    units = [{'unit_root_message_id': item['id'], 'source_message_ids': [item['id']],
              'track_id': item['track_id'], 'session_id': 1,
              'routing_role': 'bridge' if item['id'] == bridge else 'primary_activity'}
             for item in messages]
    edges = [{'unit_root_message_id': bridge, 'track_id': 'b', 'relation': 'bridge'}] if bridge else []
    return {'track_ids': [track], 'messages': messages, 'context_messages': messages,
            'memberships': units, 'context_edges': edges,
            'track_cards': [{'track_id': track}], 'parked_context_source_ids': [],
            'context_session_ids': [1], 'base_event_candidates': [],
            'base_event_candidate_overflow': []}


def test_declared_bridge_joins_two_tracks_but_needs_source_backed_continuation():
    messages = [{'id': 1, 'track_id': 'a', 'session_id': 1, 'content': 'Check the map.', 'created_at': '2026-01-01T00:00:00Z'},
                {'id': 2, 'track_id': 'a', 'session_id': 1, 'content': 'The route is clear; now inspect the sign.', 'created_at': '2026-01-01T00:01:00Z'},
                {'id': 3, 'track_id': 'b', 'session_id': 1, 'content': 'The sign has the same route.', 'created_at': '2026-01-01T00:02:00Z'}]
    left = _component('a', messages[:2], 2)
    right = _component('b', [messages[1], messages[2]], 2)
    joined = bounded_components([left, right])
    assert len(joined) == 1
    component = joined[0]
    assert component['track_ids'] == ['a', 'b']
    plan = {'events': [{'primary_track_id': 'a', 'source_message_ids': [1, 2, 3]}]}
    review = {'continuations': [{'event_index': 0, 'left_track_id': 'a', 'right_track_id': 'b',
                                 'bridge_unit_root': 2, 'reason': 'The sign follows the map check',
                                 'evidence': [{'source_message_id': 2, 'quote': 'now inspect the sign'},
                                              {'source_message_id': 3, 'quote': 'same route'}]}]}
    validate_continuations(review, plan, component)
    broken = copy.deepcopy(review)
    broken['continuations'][0]['evidence'][1]['quote'] = 'other route'
    with pytest.raises(ValueError, match='verbatim'):
        validate_continuations(broken, plan, component)


def test_unbridged_and_oversized_components_remain_separate():
    one = _component('a', [{'id': 1, 'track_id': 'a', 'session_id': 1,
                            'content': 'A' * 120_001, 'created_at': '2026-01-01T00:00:00Z'},
                           {'id': 2, 'track_id': 'a', 'session_id': 1,
                            'content': 'Bridge', 'created_at': '2026-01-01T00:01:00Z'}], 2)
    two = _component('b', [{'id': 2, 'track_id': 'a', 'session_id': 1,
                            'content': 'Bridge', 'created_at': '2026-01-01T00:01:00Z'},
                           {'id': 3, 'track_id': 'b', 'session_id': 1,
                            'content': 'B', 'created_at': '2026-01-01T00:02:00Z'}], 2)
    assert len(bounded_components([one, two])) == 2
    one['context_edges'] = []
    two['context_edges'] = []
    one['messages'][0]['content'] = 'A'
    assert len(bounded_components([one, two])) == 2


def test_bridge_owner_is_explicit_or_excluded_with_verbatim_evidence():
    messages = [
        {'id': 1, 'track_id': 'a', 'session_id': 1, 'role': 'user',
         'content': 'Compare the two notebook covers.', 'created_at': '2026-01-01T00:00:00Z'},
        {'id': 2, 'track_id': 'a', 'session_id': 1, 'role': 'user',
         'content': 'Use the same paper to sketch a label.', 'created_at': '2026-01-01T00:01:00Z'},
        {'id': 3, 'track_id': 'b', 'session_id': 1, 'role': 'assistant',
         'content': 'A small label fits the cover.', 'created_at': '2026-01-01T00:02:00Z'},
    ]
    component = bounded_components([_component('a', messages[:2], 2),
                                    _component('b', messages[1:], 2)])[0]
    output = {'events': [
        {'primary_track_id': 'a', 'source_bindings': [{'source_message_id': 1}]},
        {'primary_track_id': 'b', 'source_bindings': [
            {'source_message_id': 2}, {'source_message_id': 3}]},
    ]}
    with pytest.raises(ValueError, match='Missing bridge ownership decision'):
        validate_bridge_owners(output, component)
    exclusion = {'unit_root_message_id': 2, 'excluded_track_id': 'a',
                 'reason': 'The label starts a separate activity',
                 'evidence': [{'source_message_id': 2, 'quote': 'sketch a label'}]}
    validate_bridge_owners(output, component, {'bridge_exclusions': [exclusion]})
    invented = copy.deepcopy(exclusion)
    invented['evidence'][0]['quote'] = 'not in the original'
    with pytest.raises(ValueError, match='verbatim'):
        validate_bridge_owners(output, component, {'bridge_exclusions': [invented]})
    output['events'][0]['source_bindings'].append({'source_message_id': 2})
    validate_bridge_owners(output, component, {'bridge_exclusions': []})


@pytest.fixture
def bridge_exclusion_case():
    component = {
        'memberships': [{'unit_root_message_id': 2, 'source_message_ids': [2],
                         'track_id': 'a', 'routing_role': 'bridge'}],
        'context_edges': [{'unit_root_message_id': 2, 'track_id': 'b', 'relation': 'bridge'}],
        'messages': [{'id': 2, 'content': 'Use the same paper to sketch a label.'}],
    }
    output = {'events': [
        {'primary_track_id': 'a', 'source_bindings': [{'source_message_id': 1}]},
        {'primary_track_id': 'b', 'source_bindings': [
            {'source_message_id': 2}, {'source_message_id': 3}]},
    ]}
    exclusion = {'unit_root_message_id': 2, 'excluded_track_id': 'a',
                 'reason': 'The label starts a separate activity',
                 'evidence': [{'source_message_id': 2, 'quote': 'sketch a label'}]}
    return component, output, exclusion


@pytest.mark.parametrize('state', ['both_own', 'neither_owns', 'one_event', 'no_events'])
def test_no_missing_bridge_side_requests_empty_exclusions_without_changing_owners(bridge_exclusion_case, state):
    component, output, exclusion = bridge_exclusion_case
    if state == 'both_own':
        output['events'][0]['source_bindings'].append({'source_message_id': 2})
    elif state == 'neither_owns':
        output['events'][1]['source_bindings'] = [{'source_message_id': 3}]
    elif state == 'one_event':
        output['events'] = output['events'][1:]
    else:
        output['events'] = []
    before = copy.deepcopy(output)
    with pytest.raises(ValueError) as error:
        validate_bridge_owners(output, component, {'bridge_exclusions': [exclusion]})
    message = str(error.value)
    assert "Unexpected bridge exclusion (2, 'a')" in message
    assert 'Event ownership are []' in message
    assert 'No missing side' in message and 'bridge_exclusions: []' in message
    assert 'Do not create an Event or change ownership' in message
    assert output == before
    validate_bridge_owners(output, component, {'bridge_exclusions': []})
    validate_bridge_owners(output, component, {})
    assert output == before


@pytest.mark.parametrize('root,track', [(2, 'b'), (2, 'unknown'), (999, 'a')])
def test_wrong_bridge_exclusion_reports_the_required_pair(bridge_exclusion_case, root, track):
    component, output, exclusion = bridge_exclusion_case
    exclusion.update(unit_root_message_id=root, excluded_track_id=track)
    with pytest.raises(ValueError) as error:
        validate_bridge_owners(output, component, {'bridge_exclusions': [exclusion]})
    message = str(error.value)
    assert repr((root, track)) in message
    assert "Event ownership are [(2, 'a')]" in message
    assert 'Remove this extra exclusion entry' in message
    assert 'No missing side' not in message


def test_duplicate_bridge_exclusion_identifies_the_duplicate(bridge_exclusion_case):
    component, output, exclusion = bridge_exclusion_case
    with pytest.raises(ValueError, match=r"Duplicate bridge exclusion \(2, 'a'\)"):
        validate_bridge_owners(output, component, {'bridge_exclusions': [exclusion, exclusion]})


@pytest.mark.parametrize('root,track', [('2', 'a'), (True, 'a'), (2, ['a'])])
def test_bridge_exclusion_id_types_have_a_separate_error(bridge_exclusion_case, root, track):
    component, output, exclusion = bridge_exclusion_case
    exclusion.update(unit_root_message_id=root, excluded_track_id=track)
    with pytest.raises(ValueError, match='integer unit_root_message_id and string excluded_track_id'):
        validate_bridge_owners(output, component, {'bridge_exclusions': [exclusion]})


def test_missing_bridge_exclusion_reports_the_pair_and_available_decisions(bridge_exclusion_case):
    component, output, _ = bridge_exclusion_case
    with pytest.raises(ValueError) as error:
        validate_bridge_owners(output, component, {})
    message = str(error.value)
    assert "Missing bridge ownership decision for [(2, 'a')]" in message
    assert 'share the whole unit' in message and 'decision_review.bridge_exclusions' in message


def test_compact_curator_does_not_add_a_second_bridge_owner():
    messages = [
        {'id': 1, 'track_id': 'a', 'session_id': 1, 'role': 'user',
         'content': 'Compare the covers.', 'created_at': '2026-01-01T00:00:00Z'},
        {'id': 2, 'track_id': 'a', 'session_id': 1, 'role': 'user',
         'content': 'Now make a label.', 'created_at': '2026-01-01T00:01:00Z'},
        {'id': 3, 'track_id': 'b', 'session_id': 1, 'role': 'assistant',
         'content': 'The label can be blue.', 'created_at': '2026-01-01T00:02:00Z'},
    ]
    component = bounded_components([_component('a', messages[:2], 2),
                                    _component('b', messages[1:], 2)])[0]
    proposal = {'events': [
        {'action': 'create', 'base_event_ids': [], 'primary_track_id': 'a', 'owned_unit_roots': [1]},
        {'action': 'create', 'base_event_ids': [], 'primary_track_id': 'b', 'owned_unit_roots': [2, 3]},
    ], 'skip_unit_roots': [], 'defer_unit_roots': [],
        'decision_review': {'events': [{'event_index': 0, 'reason': 'Cover comparison'},
                                      {'event_index': 1, 'reason': 'Label design'}],
                            'boundaries': [{'left_event_index': 0, 'right_event_index': 1,
                                            'reason': 'A new design task',
                                            'evidence': [{'source_message_id': 1, 'quote': 'Compare the covers'},
                                                         {'source_message_id': 3, 'quote': 'label can be blue'}]}],
                            'dispositions': [], 'continuations': []}}
    with pytest.raises(ValueError, match='Missing bridge ownership decision'):
        normalize_event_curator_output(proposal, component)
    proposal['decision_review']['bridge_exclusions'] = [{
        'unit_root_message_id': 2, 'excluded_track_id': 'a',
        'reason': 'The message only opens label design',
        'evidence': [{'source_message_id': 2, 'quote': 'Now make a label'}],
    }]
    plan = normalize_event_curator_output(proposal, component)
    assert plan['events'][0]['source_message_ids'] == [1]
    assert plan['events'][1]['source_message_ids'] == [2, 3]


def test_shared_bridge_protection_defers_both_joint_events():
    messages = [
        {'id': 1, 'track_id': 'a', 'session_id': 1, 'role': 'user',
         'content': 'Check the notebook cover.', 'created_at': '2026-01-01T00:00:00Z'},
        {'id': 2, 'track_id': 'a', 'session_id': 1, 'role': 'user',
         'content': 'The cover is checked; now make a label.', 'created_at': '2026-01-01T00:01:00Z'},
        {'id': 3, 'track_id': 'b', 'session_id': 1, 'role': 'assistant',
         'content': 'The label can be blue.', 'created_at': '2026-01-01T00:02:00Z'},
    ]
    component = bounded_components([_component('a', messages[:2], 2),
                                    _component('b', messages[1:], 2)])[0]
    component['context_messages'].append({**messages[0], 'id': 10,
                                          'content': 'An earlier cover check.'})
    component['base_event_candidates'] = [{
        'event_id': 'old-cover', 'primary_track_id': 'a', 'session_ids': [1],
        'source_message_ids': [10], 'predecessor_event_ids': [],
        'active': True, 'protected': True,
    }]
    proposal = {'events': [
        {'action': 'extend', 'base_event_ids': ['old-cover'],
         'primary_track_id': 'a', 'owned_unit_roots': [1, 2]},
        {'action': 'create', 'base_event_ids': [],
         'primary_track_id': 'b', 'owned_unit_roots': [2, 3]},
    ], 'skip_unit_roots': [], 'defer_unit_roots': [],
        'decision_review': {'events': [{'event_index': 0, 'reason': 'Cover check'},
                                      {'event_index': 1, 'reason': 'Label design'}],
                            'boundaries': [{'left_event_index': 0, 'right_event_index': 1,
                                            'reason': 'Separate label task',
                                            'evidence': [{'source_message_id': 1,
                                                          'quote': 'Check the notebook cover'},
                                                         {'source_message_id': 3,
                                                          'quote': 'label can be blue'}]}],
                            'dispositions': [], 'continuations': []}}
    plan = normalize_event_curator_output(proposal, component)
    assert plan['events'] == []
    assert set(plan['defer_source_message_ids']) == {1, 2, 3}
