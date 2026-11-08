"""Kelivo's explicit user-message memory prefix is context, not new speech."""
from copy import deepcopy
import json

import pytest

from serein.chat_archive import original, prepare_turn
from serein.chat_context import ClientContext
from serein.compat.germany import raw_events, raw_text
from serein.extensions import pipeline
from test_chat_archive import deployment, configure, rows, send


ZH = '以下内容由系统提供，不是用户本轮发送的内容。'
EN = 'The following context is provided by the system. It is not what the user said in this turn.'


def snapshot(intro=ZH, *, empty_profile=False, empty_memory=False):
    profile = '<user_profile/>' if empty_profile else '<user_profile><occupation>SYNTHETIC_PROFILE</occupation></user_profile>'
    blocks = []
    for kind in ('identity', 'workflow', 'voice', 'instruction'):
        blocks.append(f'<user_memory type="{kind}"/>' if empty_memory else
                      f'<user_memory type="{kind}" mode="summary" total="20" shown="10">\n'
                      '- [2026-10-29] SYNTHETIC_MEMORY &lt;not-speech&gt;\n</user_memory>')
    return intro+'\n'+profile+'\n'+'\n'.join(blocks)+'\n\n'


@pytest.mark.parametrize('cleaner', [raw_text.strip_raw_client_context, raw_events.strip_raw_client_context,
                                   lambda text: original({'role': 'user', 'content': text})['text']])
@pytest.mark.parametrize('prefix', [snapshot(), snapshot(EN), snapshot(empty_profile=True), snapshot(empty_memory=True),
    '以下是本次对话开始后发生的记忆更新，由系统提供。\n<user_memory_update><added>SYNTHETIC_UPDATE</added></user_memory_update>\n\n',
    'The following memory changes happened after this conversation started, provided by the system.\n<user_memory_update><added>SYNTHETIC_UPDATE</added></user_memory_update>\n\n'])
def test_only_the_complete_leading_client_prefix_is_removed(cleaner, prefix):
    assert cleaner(prefix+'Actual question') == 'Actual question'
    assert cleaner(prefix) == ''
    assert cleaner(cleaner(prefix+'Actual question')) == 'Actual question'


@pytest.mark.parametrize('text', [
    'Please explain this example:\n'+snapshot(),
    '```xml\n'+snapshot()+'```',
    '<user_memory type="identity">My literal example</user_memory>',
    ZH+'\n<user_profile/>\n<user_memory type="identity">unfinished',
    ZH+'\n<user_profile/>\nActual speech without a complete generated envelope',
    'Ordinary dialogue',
])
def test_quoted_arbitrary_and_incomplete_envelopes_are_preserved(text):
    assert raw_text.strip_kelivo_memory_context(text) == text


def test_split_content_blocks_images_and_turn_identity_keep_only_authored_speech():
    prefix = snapshot()
    profile_end = prefix.index('</user_profile>')+len('</user_profile>')
    image = {'type': 'image_url', 'image_url': {'url': 'https://synthetic.invalid/image.png'}}
    message = {'role': 'user', 'id': 'same-question', 'content': [
        {'type': 'text', 'text': prefix[:profile_end]},
        {'type': 'input_text', 'input_text': prefix[profile_end:]+'Actual question'}, image]}
    before = deepcopy(message)
    projected = original(message)
    assert projected['text'] == 'Actual question'
    assert projected['attachments'] == [{'kind': 'image', 'url': image['image_url']['url']}]
    for enabled in (False, True):
        assert ClientContext(operit=enabled)._extract_current_turn_user_query([message]) == 'Actual question'
    assert message == before
    first = prepare_turn('kelivo', [message])
    changed = deepcopy(message)
    changed['content'][1]['input_text'] = changed['content'][1]['input_text'].replace('SYNTHETIC_MEMORY', 'CHANGED_MEMORY')
    assert prepare_turn('kelivo', [changed])['key'] == first['key']
    assert prepare_turn('kelivo', [{'role': 'user', 'content': prefix}]) is None


def test_real_chat_preserves_upstream_context_but_archives_no_kelivo_memory(deployment, monkeypatch):
    settings, client = deployment
    configure(client).raise_for_status()
    prefix = snapshot().replace('SYNTHETIC_MEMORY', 'MEMORY_SENTINEL'+'旧'*15000)
    incoming = {'role': 'user', 'id': 'kelivo-question', 'content': prefix+'Actual question'}
    before = deepcopy(incoming)
    forwarded = []
    async def complete(model, body, **kwargs):
        forwarded.append(deepcopy(body))
        expected_prefix = prefix if len(forwarded) == 1 else prefix.replace('MEMORY_SENTINEL', 'UPDATED_MEMORY_SENTINEL')
        assert body['messages'][-1]['content'] == expected_prefix+'Actual question'
        return {'choices': [{'message': {'role': 'assistant', 'content': 'Actual answer'}}]}
    monkeypatch.setattr('serein.api.chat.complete', complete)
    send(client, [incoming]).raise_for_status()
    assert incoming == before
    assert [(row['role'], row['text']) for row in rows(settings)] == [('user', 'Actual question'), ('assistant', 'Actual answer')]
    pipeline.initialize(settings.database)
    batch = pipeline.new_batch(settings.database, True)
    assert 'MEMORY_SENTINEL' not in batch['input_json']
    assert json.loads(batch['input_json'])['messages'][0]['content'] == 'Actual question'
    # Changing a memory snapshot on a retried turn must not create new raw speech.
    changed = {**incoming, 'content': incoming['content'].replace('MEMORY_SENTINEL', 'UPDATED_MEMORY_SENTINEL')}
    send(client, [changed]).raise_for_status()
    assert len(rows(settings)) == 2 and len(forwarded) == 2
