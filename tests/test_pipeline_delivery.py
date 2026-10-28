"""Synthetic Issue #76 coverage: read once, bounded supplements, full host proof."""
import asyncio
import copy
import json

import pytest

from test_public_features import settings, ingest, output_for, synthetic_runner
from serein.core.store import Store, encode
from serein.deployment import save_settings
from serein.extensions import pipeline as p, pipeline_latest as latest


def material(sid, use='main'):
    return {'source_message_id': sid, 'use': use, 'reason': 'Synthetic activity', 'omit_quotes': []}


def component_fixture():
    def message(sid, old=False):
        return {'id': sid, 'session_id': 1, 'original_session_id': 'synthetic', 'source': 'synthetic',
                'role': 'user' if sid % 2 else 'assistant',
                'content': (f'OLD_ORIGINAL_{sid}: ' + '材料' * 100) if old else f'NEW_ORIGINAL_{sid}: ' + '新材料' * 50,
                'created_at': '2026-10-22T00:00:00Z', 'metadata': {}}
    history = [message(i, True) for i in range(1, 152)]
    current = [message(i) for i in range(1001, 1008)]
    current[0]['role'] = 'assistant'; current[0]['metadata'] = {'proactive': True}
    current[1]['role'] = 'user'; current[2]['role'] = 'assistant'
    for i, row in enumerate(current[3:]):
        row['role'] = 'user' if i % 2 == 0 else 'assistant'
    bases = [{'event_id': name, 'primary_track_id': 'track', 'session_ids': [1],
              'title': name, 'body': f'Saved summary for {name}', 'source_message_ids': ids,
              'active': True} for name, ids in (
                  ('a', list(range(1, 39))), ('b', list(range(37, 149))), ('c', [149, 150, 151]))]
    return {'curator_summary_first': True, 'track_ids': ['track'], 'track_cards': [{'track_id': 'track', 'event_policy': 'default'}],
            'messages': current, 'context_messages': [*history, *current], 'context_session_ids': [1],
            'memberships': [{'unit_root_message_id': ids[0], 'source_message_ids': ids,
                             'track_id': 'track', 'session_id': 1, 'routing_role': 'primary_activity'}
                            for ids in ([1001, 1002, 1003], [1004, 1005], [1006, 1007])],
            'context_edges': [], 'parked_context_source_ids': [], 'base_event_candidates': bases}


def batch_fixture(settings, component):
    p.initialize(settings.database)
    data = {'day': '2026-10-22', 'contract': p.CONTRACT, 'runtime_revision': p.runtime_revision()}
    batch = {'id': 'synthetic-delivery', 'scope': 'synthetic', 'input_json': encode(data)}
    with Store(settings.database) as store:
        store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json) VALUES (?,?,?)',
                           (batch['id'], batch['scope'], batch['input_json']))
    return batch


def proposed(action='create', bases=None):
    return {'events': [{'action': action, 'base_event_ids': bases or [], 'primary_track_id': 'track',
                        'owned_unit_roots': [1001, 1004, 1006]}],
            'skip_unit_roots': [], 'defer_unit_roots': [],
            'decision_review': {'events': [{'event_index': 0, 'reason': 'Synthetic activity'}],
                                'boundaries': [], 'dispositions': []}}


def tag(prompt, name):
    return json.loads(prompt.split(f'<{name}>\n', 1)[1].split(f'\n</{name}>', 1)[0])


def test_curator_mcp_packet_has_one_copy_of_current_text_and_no_old_originals(settings):
    component = component_fixture(); before = copy.deepcopy(component)
    batch = batch_fixture(settings, component)
    request = p.request_for(settings.database, batch, 'event_curator', component=component)
    rendered = tag(request['prompt'], 'event_curator_input_json')
    assert [base['body'] for base in rendered['base_events']] == [b['body'] for b in component['base_event_candidates']]
    assert all('messages' not in base for base in rendered['base_events'])
    view = p.writer_task_view(request); packet = encode(view)
    assert 'OLD_ORIGINAL_' not in packet
    for row in component['messages']:
        assert packet.count(row['content']) == 1
    assert component == before and len(request['component']['context_messages']) == 158
    assert len(packet) < 30000
    for base in rendered['base_events']:
        assert base['source_message_ids'] == next(b['source_message_ids'] for b in component['base_event_candidates'] if b['event_id'] == base['event_id'])


def test_reduction_defaults_off_and_preserves_full_old_reading_until_enabled(settings):
    from serein.deployment import read_settings
    assert read_settings(settings.database)['pipeline']['curator_summary_first'] is False
    component = component_fixture()
    component['curator_summary_first'] = False
    batch = batch_fixture(settings, component)
    full = p.request_for(settings.database, batch, 'event_curator', component=component)
    assert 'curator_read_source_ids' not in full['component']
    base = tag(full['prompt'], 'event_curator_input_json')['base_events'][0]
    assert [m['source_message_id'] for m in base['messages']] == list(range(1, 39))
    assert 'OLD_ORIGINAL_1:' in full['prompt']
    assert '减小输入已关闭' in full['prompt']
    save_settings(settings.database, {'pipeline': {'curator_summary_first': True}})
    assert read_settings(settings.database)['pipeline']['curator_summary_first'] is True
    component['curator_summary_first'] = True
    small = p.request_for(settings.database, batch, 'event_curator', component=component)
    assert 'OLD_ORIGINAL_' not in small['prompt']
    assert small['component']['base_event_candidates'] == full['component']['base_event_candidates']
    for invalid in (1, 'false', None):
        with pytest.raises(ValueError, match='boolean'):
            save_settings(settings.database, {'pipeline': {'curator_summary_first': invalid}})


def test_agent_returns_compact_packet_but_submission_uses_full_host_snapshot(settings):
    ingest(settings)
    task = asyncio.run(p.advance(settings.database, include_recent=True))
    while task['role'] != 'event_writer':
        request = task['request']
        if task['role'] == 'event_curator':
            assert all('content' not in row for row in request['component']['context_messages'])
        p.submit(settings.database, task['job_id'], output_for(task['role'], request))
        task = asyncio.run(p.advance(settings.database, include_recent=True))
    view = task['request']
    assert all('content' not in row for row in view['messages'])
    text = tag(view['prompt'], 'event_reading_block_json')[0]['text']
    assert encode(view).count(text) == 1
    with Store(settings.database, read_only=True) as store:
        full = json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?', (task['job_id'],)).fetchone()[0])
    assert full['messages'][0]['content'] == text
    bad = output_for('event_writer', view)
    bad['claim_groups'][0]['source_spans'][0]['quote'] = 'INVENTED ORIGINAL'
    with pytest.raises(ValueError):
        p.submit(settings.database, task['job_id'], bad)
    p.submit(settings.database, task['job_id'], output_for('event_writer', view))
    assert asyncio.run(p.advance(settings.database, include_recent=True))['events'] == 1


def test_candidate_supplement_is_complete_scoped_and_bounded(settings):
    component = component_fixture(); batch = batch_fixture(settings, component)
    request = p.request_for(settings.database, batch, 'event_curator', component=component)
    query = {'event_id': 'b', 'reason': 'missing_prior_claim'}
    p.validate(request, {'context_request': query})
    context = p.extend_context(settings.database, request['component'], query, curator=True)
    receipt = context['context_receipt']
    assert len(receipt['read_source_ids']) == 12
    assert set(receipt['read_source_ids']).issubset(component['base_event_candidates'][1]['source_message_ids'])
    reread = p.request_for(settings.database, batch, 'event_curator', component=context, context_read=True)
    rendered = tag(reread['prompt'], 'event_curator_input_json')
    read = [m for unit in rendered['units'] if unit['scope'] == 'context_only' for m in unit['messages']]
    assert [m['source_message_id'] for m in read] == receipt['read_source_ids']
    assert len(encode(read)) <= latest.CURATOR_CONTEXT_MAX_CHARS
    assert any('OLD_ORIGINAL_' in m['text'] for m in read)
    for change in ({'event_id': 'foreign'}, {'before_message_id': True}, {'reason': 'invented'}):
        with pytest.raises(ValueError):
            p.validate(request, {'context_request': {**query, **change}})
    with pytest.raises(ValueError, match='已补读一次'):
        p.validate(reread, {'context_request': query})


def test_oversized_unit_is_skipped_whole_without_truncating_originals(settings):
    component = component_fixture(); original = component['context_messages'][147]
    original['content'] = 'HUGE_ORIGINAL' * 5000
    before = copy.deepcopy(component)
    context = p.extend_context(settings.database, component,
        {'event_id': 'b', 'reason': 'missing_origin'}, curator=True)
    assert 147 in context['context_receipt']['omitted_unit_roots']
    assert not {147, 148} & set(context['context_receipt']['read_source_ids'])
    assert component == before


def test_partial_annotations_keep_full_ownership_and_unread_sources_are_not_omit(settings):
    component = component_fixture(); batch = batch_fixture(settings, component)
    component['base_event_candidates'][1]['source_message_ids'] = list(range(39,149))
    component.update(writer_material_review=True, writer_round_gate=True)
    component = p.request_for(settings.database, batch, 'event_curator', component=component)['component']
    raw = proposed('extend', ['a'])
    raw['decision_review']['events'][0].update(admission={'closed_by': None},
        materials=[material(row['id']) for row in component['messages']])
    normalized = latest.normalize_event_curator_output(raw, component)
    event, = normalized['events']
    assert set(event['source_message_ids']) == set(range(1, 39)) | set(range(1001, 1008))
    assert event['unreviewed_source_message_ids'] == list(range(1, 39))
    bad = copy.deepcopy(raw); bad['decision_review']['events'][0]['materials'].append(material(1))
    with pytest.raises(ValueError, match='unexpected'):
        latest.normalize_event_curator_output(bad, component)
    # Keep confirmed choices while explicitly retaining the unreviewed remainder.
    from serein.extensions.pipeline_materials import snapshot_for, substantive_ids, trusted_materials
    receipt=snapshot_for(event, component['context_messages'])
    assert receipt['unreviewed_source_message_ids']==list(range(1,39))
    base={**component['base_event_candidates'][0],'source_message_ids':event['source_message_ids'],
          'event_id':'successor','fingerprint':'synthetic-version'}
    base['material_snapshot']={**receipt,'event_id':'successor','event_fingerprint':'synthetic-version'}
    assert trusted_materials(base,component['context_messages']) is None
    assert trusted_materials(base,component['context_messages'],allow_partial=True)==event['source_materials']
    assert set(substantive_ids(event)) == set(event['source_message_ids'])


@pytest.mark.parametrize('action,bases', [('create', []), ('extend', ['a']), ('rewrite', ['a']), ('merge', ['a', 'c'])])
def test_writer_retains_required_old_evidence_only_for_rewrite_and_merge(settings, action, bases):
    component = component_fixture(); batch = batch_fixture(settings, component)
    component['base_event_candidates'][1]['source_message_ids'] = list(range(39,149))
    event, = latest.normalize_event_curator_output(proposed(action, bases), component)['events']
    request = p.request_for(settings.database, batch, 'event_writer', event=event, component=component)
    block = tag(request['prompt'], 'event_reading_block_json')
    ids = {row['source_message_id'] for row in block}
    expected = {m['id'] for m in component['messages']}
    if action in ('rewrite', 'merge'):
        expected |= {sid for b in component['base_event_candidates'] if b['event_id'] in bases for sid in b['source_message_ids']}
    assert ids == expected
    view = p.writer_task_view(request)
    for row in block:
        assert encode(view).count(row['text']) == 1


def test_agent_budget_measures_serialized_packet_not_just_prompt(settings):
    component = component_fixture(); batch = batch_fixture(settings, component)
    request = p.request_for(settings.database, batch, 'event_curator', component=component)
    # Escaping and structure make a large real packet even with a short prompt.
    request['prompt'] = '\\"' * 12000
    limit = len(request['rules']) + len(request['prompt']) + 100
    save_settings(settings.database, {'pipeline': {'execution_mode': 'agent', 'max_prompt_chars': limit}})
    with pytest.raises(ValueError, match='完整返回包'):
        asyncio.run(p.job(settings.database, batch, request, 'event_curator:large', None))
    with Store(settings.database, read_only=True) as store:
        saved = json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE batch_id=?', (batch['id'],)).fetchone()[0])
    assert saved['prompt'] == request['prompt']


def test_real_mcp_next_returns_single_text_with_no_structured_duplicate(settings):
    from serein.application import Application
    from serein.api.mcp import create_server
    save_settings(settings.database, {'features': {'pipeline_agent': True}})
    ingest(settings)
    server = create_server(Application(settings))
    tools = asyncio.run(server.list_tools())
    assert next(tool for tool in tools if tool.name == 'pipeline_next').outputSchema is None
    for role in p.ROLES:
        content = asyncio.run(server.call_tool('pipeline_next', {'include_recent': True}))
        assert isinstance(content, list) and len(content) == 1 and content[0].type == 'text'
        task = json.loads(content[0].text)
        assert task['role'] == role
        if role == 'event_writer':
            text = tag(task['request']['prompt'], 'event_reading_block_json')[0]['text']
            assert content[0].text.count(text) == 1
        p.submit(settings.database, task['job_id'], output_for(role, task['request']))
    assert json.loads(asyncio.run(server.call_tool('pipeline_next', {'include_recent': True}))[0].text)['events'] == 1


def test_mcp_wrapper_checks_escaped_text_envelope(settings, monkeypatch):
    from serein.application import Application
    from serein.api.mcp import create_server
    save_settings(settings.database, {'features': {'pipeline_agent': True}})
    async def task(*args, **kwargs):
        return {'request': {'prompt': '\\"' * 2000}, 'delivery_limit_chars': 10000}
    monkeypatch.setattr(p, 'advance', task)
    server = create_server(Application(settings))
    with pytest.raises(Exception, match='MCP 完整工具结果'):
        asyncio.run(server.call_tool('pipeline_next', {}))


def test_mcp_completion_receipt_does_not_consume_model_prompt_budget(settings, monkeypatch):
    from serein.application import Application
    from serein.api.mcp import create_server
    save_settings(settings.database, {'features': {'pipeline_agent': True},
                                      'pipeline': {'max_prompt_chars': 1}})
    async def current(*args, **kwargs):
        return {'status': 'current', 'events': 0}
    monkeypatch.setattr(p, 'advance', current)
    content = asyncio.run(create_server(Application(settings)).call_tool('pipeline_next', {}))
    assert json.loads(content[0].text) == {'status': 'current', 'events': 0}


def test_both_mcp_entries_preserve_original_data_uri_and_emit_image_once(settings, monkeypatch):
    from serein.application import Application
    from serein.api.mcp import create_server
    from test_event_handoff import PNG
    import importlib.util
    from pathlib import Path
    result = {'status': 'awaiting_agent', 'request': {'prompt': 'Original literal: ' + PNG,
        'images': [{'url': PNG, 'source_message_id': 1, 'position': 1}]}}
    async def task(*args, **kwargs):
        return copy.deepcopy(result)
    save_settings(settings.database, {'features': {'pipeline_agent': True}})
    monkeypatch.setattr(p, 'advance', task)
    content = asyncio.run(create_server(Application(settings)).call_tool('pipeline_next', {}))
    spec = importlib.util.spec_from_file_location('delivery_adapter', Path(__file__).parents[1]/'scripts/event_agent_mcp.py')
    adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
    monkeypatch.setattr(adapter, 'call', lambda *args: copy.deepcopy(result))
    forwarded = asyncio.run(adapter.server.call_tool('pipeline_next', {}))
    for output in (content, forwarded):
        assert isinstance(output, list) and [item.type for item in output] == ['text', 'image']
        assert output[0].text.count(PNG) == 1
        view = json.loads(output[0].text)
        assert view['request']['prompt'] == result['request']['prompt']
        assert 'url' not in view['request']['images'][0]
        assert output[1].data == PNG.split(',', 1)[1]
    assert result['request']['images'][0]['url'] == PNG


def test_old_boundary_quote_is_unavailable_until_the_original_is_supplied(settings):
    component = component_fixture(); batch = batch_fixture(settings, component)
    component['base_event_candidates'][1]['source_message_ids'] = list(range(39,149))
    request = p.request_for(settings.database, batch, 'event_curator', component=component)
    raw = proposed('extend', ['a']); raw['events'][0]['owned_unit_roots'] = [1001, 1004]
    raw['events'].append({'action': 'create', 'base_event_ids': [], 'primary_track_id': 'track', 'owned_unit_roots': [1006]})
    raw['decision_review']['events'].append({'event_index': 1, 'reason': 'Independent synthetic activity'})
    by_id = {m['id']: m for m in component['context_messages']}
    raw['decision_review']['boundaries'] = [{'left_event_index': 0, 'right_event_index': 1, 'reason': 'Independent activities',
        'evidence': [{'source_message_id': 27, 'quote': by_id[27]['content']},
                     {'source_message_id': 1006, 'quote': by_id[1006]['content']}]}]
    with pytest.raises(ValueError, match='boundary'):
        p.validate(request, copy.deepcopy(raw))
    context = p.extend_context(settings.database, request['component'], {'event_id': 'a', 'reason': 'missing_prior_claim'}, curator=True)
    reread = p.request_for(settings.database, batch, 'event_curator', component=context, context_read=True)
    assert 27 in reread['component']['curator_read_source_ids']
    p.validate(reread, raw)


def test_rewrite_loads_selected_old_image_evidence_after_summary_only_curator(settings):
    from test_event_handoff import PNG
    from serein.compat.raw_archive import raw_archive
    raw_archive(settings).ingest([
        {'source_event_id': 'u1', 'session_id': 'books', 'role': 'user', 'text': 'Book club plan 1',
         'created_at': '2025-01-01T00:00:00Z', 'metadata': {'attachments': [{'kind': 'image', 'url': PNG}]}},
        {'source_event_id': 'a1', 'session_id': 'books', 'role': 'assistant', 'text': 'Agreed to plan 1',
         'created_at': '2025-01-01T00:01:00Z'}], source='test')
    save_settings(settings.database, {'pipeline': {'curator_summary_first': True}})
    images = []; writers = []
    async def runner(role, request):
        if request.get('transcription_only'):
            images.append(request['images'][0]['source_message_id'])
            return {'image_transcriptions': [{'input_image': 1, 'text': 'IMAGE_EVIDENCE_SENTINEL', 'unreadable': False}]}
        result = output_for(role, request)
        if role == 'event_curator' and request['component']['base_event_candidates']:
            assert 'IMAGE_EVIDENCE_SENTINEL' not in request['prompt']
            result['events'][0]['action'] = 'rewrite'
        if role == 'event_writer':
            writers.append(copy.deepcopy(request))
        return result
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['events'] == 1
    ingest(settings, 2)
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['events'] == 1
    assert images == [1]  # Selected old evidence reuses its exact cached transcription.
    assert {m['id'] for m in writers[-1]['messages']} == {1, 2, 3, 4}
    assert 'IMAGE_EVIDENCE_SENTINEL' in writers[-1]['prompt']
    assert writers[-1]['images'] == []
