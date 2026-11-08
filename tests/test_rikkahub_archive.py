"""RikkaHub's generated time reminders are model context, not dialogue."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from serein.chat_archive import original, prepare_turn, archive_turn
from serein.chat_context import ClientContext
from serein.compat.germany import raw_events, raw_text
from serein.compat.raw_archive import raw_archive
from serein.extensions import pipeline
from test_chat_archive import deployment, configure, rows, send


REMINDER = '<time_reminder>Current time: Thursday, 2026-10-29T20:00 (2 h since last message)</time_reminder>'
CHANGED = REMINDER.replace('Thursday', '星期四').replace('20:00', '20:01')
IMAGE = {'type': 'image_url', 'image_url': {'url': 'https://synthetic.invalid/rikka.png'}}


@pytest.mark.parametrize('cleaner', [raw_events.strip_raw_client_context, raw_text.strip_raw_client_context,
                                   lambda text: original({'role': 'user', 'content': text})['text']])
@pytest.mark.parametrize('prefix', [REMINDER, CHANGED, REMINDER+'\n'+CHANGED])
def test_complete_leading_reminders_are_excluded(cleaner, prefix):
    assert cleaner(prefix) == ''
    assert cleaner(prefix+'\nActual question') == 'Actual question'
    assert cleaner(cleaner(prefix+'\nActual question')) == 'Actual question'


@pytest.mark.parametrize('text', [
    'Explain this example:\n'+REMINDER,
    '```xml\n'+REMINDER+'\n```',
    '<time_reminder>My literal example</time_reminder>',
    '<time_reminders>Current time: example</time_reminders>',
    '<time_reminder>Current time: unfinished',
    '<time_reminder>Current time: <nested>example</nested></time_reminder>',
    '<UploadFile name="synthetic.txt">\nFile body\n</UploadFile>\nQuestion',
    'Ordinary question',
])
def test_quoted_arbitrary_and_incomplete_reminders_are_preserved(text):
    assert raw_text.strip_rikkahub_time_context(text) == text
    assert original({'role': 'user', 'content': text})['text'] == text


def test_split_text_and_image_keep_authored_speech_without_mutation():
    message = {'role': 'user', 'content': [
        {'type': 'text', 'text': REMINDER[:REMINDER.index('Current time:')]},
        {'type': 'input_text', 'input_text': REMINDER[REMINDER.index('Current time:'):]+'\nActual question'}, IMAGE]}
    before = deepcopy(message)
    assert original(message) == {'role': 'user', 'text': 'Actual question',
                                 'attachments': [{'kind': 'image', 'url': IMAGE['image_url']['url']}]}
    for enabled in (False, True):
        assert ClientContext(operit=enabled)._extract_current_turn_user_query([message]) == 'Actual question'
    assert message == before
    assert original({'role': 'user', 'content': [{'type': 'text', 'text': REMINDER}, IMAGE]})['text'] == '[图片]'


def test_standalone_reminders_do_not_archive_or_reuse_a_previous_turn():
    history = [{'role': 'user', 'content': 'Old question'}, {'role': 'assistant', 'content': 'Old answer'}]
    reminder = {'role': 'user', 'content': REMINDER}
    assert prepare_turn('rikka', [reminder]) is None
    assert prepare_turn('rikka', [*history, reminder]) is None
    for enabled in (False, True):
        assert ClientContext(operit=enabled)._extract_current_turn_user_query([reminder]) == ''


def test_reminder_changes_do_not_duplicate_a_turn_but_later_speech_remains_distinct(tmp_path):
    settings = SimpleNamespace(database=tmp_path/'synthetic.db')
    user = {'role': 'user', 'content': 'Same question'}
    incoming = [{'role': 'user', 'content': REMINDER}, user]
    changed = [{'role': 'user', 'content': CHANGED}, user]
    first = prepare_turn('rikka', incoming)
    second = prepare_turn('rikka', changed)
    assert first['key'] == second['key']
    reply = {'role': 'assistant', 'content': 'Same answer'}
    assert archive_turn(settings, first, reply)['inserted'] == 2
    assert archive_turn(settings, second, reply)['duplicate'] == 2
    assert sorted(row['text'] for row in raw_archive(settings).search()['items']) == ['Same answer', 'Same question']
    later = prepare_turn('rikka', [*incoming, reply, {'role': 'user', 'content': CHANGED}, user])
    assert later['key'] != first['key']
    assert archive_turn(settings, later, reply)['inserted'] == 2


def test_direct_raw_ingest_rejects_reminders_and_preserves_question(tmp_path):
    archive = raw_events.RawEventStore({'raw_events': {'db_path': str(tmp_path/'raw.db')}})
    result = archive.ingest([{'role': 'user', 'id': 'reminder', 'text': REMINDER},
                             {'role': 'user', 'id': 'speech', 'text': REMINDER+'\nActual question'}])
    assert (result['inserted'], result['rejected']) == (1, 1)
    assert result['items'][0]['reason'] == 'empty_text'
    assert archive.get_event(result['items'][1]['id'])['text'] == 'Actual question'
    assert archive.search('time_reminder')['items'] == []


def test_http_tool_continuation_preserves_upstream_reminders_and_archives_only_dialogue(deployment, monkeypatch):
    settings, client = deployment
    configure(client).raise_for_status()
    user = {'role': 'user', 'content': 'Use a tool'}
    tool_call = {'role': 'assistant', 'content': 'Looking', 'tool_calls': [
        {'id': 'synthetic-call', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]}
    incoming = [{'role': 'system', 'content': 'SYNTHETIC_WORLD_BOOK'},
                {'role': 'user', 'content': REMINDER}, user]
    before = deepcopy(incoming)
    forwarded = []
    async def complete(model, body, **kwargs):
        forwarded.append(deepcopy(body))
        return {'choices': [{'message': tool_call if len(forwarded) == 1 else
                             {'role': 'assistant', 'content': 'Actual answer'}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    send(client, incoming).raise_for_status()
    assert [row['text'] for row in rows(settings)] == ['Use a tool']
    continuation = [incoming[0], {'role': 'user', 'content': CHANGED}, user, tool_call,
                    {'role': 'tool', 'tool_call_id': 'synthetic-call', 'content': 'SYNTHETIC_TOOL_MEMORY'}]
    send(client, continuation).raise_for_status()
    assert [row['text'] for row in rows(settings)] == ['Use a tool', 'Actual answer']
    assert REMINDER in json.dumps(forwarded[0]) and CHANGED in json.dumps(forwarded[1], ensure_ascii=False)
    assert 'SYNTHETIC_WORLD_BOOK' in json.dumps(forwarded[1])
    assert 'SYNTHETIC_TOOL_MEMORY' in json.dumps(forwarded[1])
    assert incoming == before
    pipeline.initialize(settings.database)
    batch = pipeline.new_batch(settings.database, True)
    assert json.loads(batch['input_json'])['messages'][0]['content'] == 'Use a tool'
    assert all(marker not in batch['input_json'] for marker in ('time_reminder', 'SYNTHETIC_WORLD_BOOK', 'SYNTHETIC_TOOL_MEMORY'))
