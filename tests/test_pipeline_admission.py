"""Synthetic complete-exchange admission without special activity sources."""

from serein.extensions.pipeline_admission import add_lookahead, apply_gate, count_rounds, validate


def _messages():
    return [{'id': 1, 'session_id': 1, 'role': 'user', 'content': 'Should we bind the notebook?'},
            {'id': 2, 'session_id': 1, 'role': 'assistant', 'content': 'The cover is sturdy enough.'},
            {'id': 3, 'session_id': 1, 'role': 'user', 'content': 'Use the blue thread.'},
            {'id': 4, 'session_id': 1, 'role': 'assistant', 'content': 'I will measure it first.'}]


def _plan(source_ids, admission):
    return {'events': [{'event_ref': 'event:0', 'source_message_ids': source_ids}],
            'event_admissions': {'event:0': admission},
            'skip_source_message_ids': [], 'defer_source_message_ids': []}


def test_two_complete_owned_exchanges_write_and_one_open_exchange_waits():
    messages = _messages()
    assert count_rounds([1, 2, 3, 4], messages) == 2
    assert count_rounds([1, 2], messages) == 1
    component = {'writer_round_gate': True, 'messages': messages,
                 'context_messages': messages}
    accepted = apply_gate(_plan([1, 2, 3, 4], {'closed_by': None}), component)
    assert len(accepted['events']) == 1
    assert accepted['admission_receipts'][0]['disposition'] == 'write'
    waiting = apply_gate(_plan([1, 2], {'closed_by': None}), component)
    assert waiting['events'] == []
    assert waiting['defer_source_message_ids'] == [1, 2]


def test_explicitly_closed_short_exchange_is_skipped_with_verbatim_receipt():
    messages = _messages()[:2]
    component = {'writer_round_gate': True, 'messages': messages,
                 'context_messages': messages}
    closure = {'source_message_id': 2, 'quote': 'cover is sturdy enough'}
    output = {'events': [{'event_ref': 'event:0', 'source_bindings': [
        {'source_message_id': 1}, {'source_message_id': 2}]}]}
    review = {'events': [{'event_index': 0, 'admission': {'closed_by': closure}}]}
    assert validate(review, output, component) == {'event:0': {'closed_by': closure}}
    result = apply_gate(_plan([1, 2], {'closed_by': closure}), component)
    assert result['events'] == []
    assert result['skip_source_message_ids'] == [1, 2]
    closure['quote'] = 'a different sentence'
    try:
        validate(review, output, component)
    except ValueError as error:
        assert 'quote' in str(error)
    else:
        raise AssertionError('Unverifiable closure must be rejected')


def test_background_and_split_bubbles_do_not_create_extra_rounds():
    messages = _messages()
    messages.insert(1, {'id': 5, 'session_id': 1, 'role': 'user', 'content': 'The thread is on the table.'})
    assert count_rounds([1, 2, 3, 4, 5], messages) == 2
    event = {'event_ref': 'event:0', 'source_message_ids': [1, 2, 3, 4, 5],
             'source_materials': [{'source_message_id': source_id,
                                   'use': 'background' if source_id in {1, 2} else 'main'}
                                  for source_id in [1, 2, 3, 4, 5]]}
    component = {'writer_round_gate': True, 'messages': messages,
                 'context_messages': messages}
    plan = {'events': [event], 'event_admissions': {'event:0': {'closed_by': None}},
            'skip_source_message_ids': [], 'defer_source_message_ids': []}
    result = apply_gate(plan, component)
    assert result['events'] == []
    assert result['admission_receipts'][0]['rounds'] == 1


def test_lookahead_is_bounded_to_later_originals_in_same_session():
    first = {'messages': _messages()[:2], 'context_messages': _messages()[:2]}
    second = {'messages': _messages()[2:], 'context_messages': _messages()[2:] + [
        {'id': 5, 'session_id': 2, 'role': 'user', 'content': 'Another session'}]}
    add_lookahead([first, second])
    assert [row['id'] for row in first['boundary_lookahead']] == [3, 4]
    assert [row['id'] for row in first['messages']] == [1, 2]
    assert [row['id'] for row in second['boundary_lookahead']] == []


def test_reply_to_another_user_cannot_erase_completed_owned_rounds():
    messages = _messages()
    messages[1]['metadata'] = {'reply_to_user_message_id': 1}
    messages[3]['metadata'] = {'reply_to_user_message_id': 3}
    context = {'id': 6, 'session_id': 1, 'role': 'assistant',
               'content': 'Reply to a different, absent user message.',
               'metadata': {'reply_to_user_message_id': 5}}
    component = {'writer_round_gate': True, 'messages': messages,
                 'context_messages': [*messages, context]}
    result = apply_gate(_plan([1, 2, 3, 4], {'closed_by': None}), component)
    assert result['admission_receipts'][0]['rounds'] == 2
    assert len(result['events']) == 1
    assert result['skip_source_message_ids'] == result['defer_source_message_ids'] == []


def test_orphan_reply_cannot_become_a_proactive_exchange_with_a_later_user():
    messages = [{'id': 2, 'session_id': 1, 'role': 'assistant',
                 'metadata': {'reply_to_user_message_id': 1}},
                {'id': 3, 'session_id': 1, 'role': 'user'}]
    assert count_rounds([2, 3], messages) == 0
    messages.append({'id': 4, 'session_id': 1, 'role': 'assistant',
                     'metadata': {'reply_to_user_message_id': 3}})
    assert count_rounds([3, 4], messages) == 1


def test_split_reply_bubbles_still_require_the_whole_reply():
    messages = [{'id': 1, 'session_id': 1, 'role': 'user'},
                {'id': 2, 'session_id': 1, 'role': 'assistant',
                 'metadata': {'reply_to_user_message_id': 1}},
                {'id': 3, 'session_id': 1, 'role': 'assistant',
                 'metadata': {'reply_to_user_message_id': 1}}]
    assert count_rounds([1, 2, 3], messages) == 1
    assert count_rounds([1, 2], messages) == 0


def test_proactive_share_stays_separate_even_with_an_old_reply_anchor():
    messages = _messages()[:2]
    messages += [{'id': 3, 'session_id': 1, 'role': 'assistant',
                  'metadata': {'proactive': True, 'reply_to_user_message_id': 1}},
                 {'id': 4, 'session_id': 1, 'role': 'user'}]
    assert count_rounds([1, 2], messages) == 1
    assert count_rounds([3, 4], messages) == 1
    assert count_rounds([1, 2, 3, 4], messages) == 2


def test_context_questions_are_not_removed_to_join_unrelated_owned_messages():
    messages = [{'id': 1, 'session_id': 1, 'role': 'user'},
                {'id': 2, 'session_id': 1, 'role': 'assistant',
                 'metadata': {'reply_to_user_message_id': 1}},
                {'id': 3, 'session_id': 1, 'role': 'user'},
                {'id': 4, 'session_id': 1, 'role': 'assistant',
                 'metadata': {'reply_to_user_message_id': 3}}]
    assert count_rounds([1, 4], messages) == 0
