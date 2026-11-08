"""Only synthetic, local databases. No private deployment or model requests."""
import asyncio
from dataclasses import replace
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from serein.config import Settings
from serein.bootstrap import initialize
from serein.core.store import Store, Conflict, digest, encode
from serein.core.reader import Reader
from serein.core.feelings import Feelings
from serein.core.plans import Plans
from serein.core.personal import Personal
from serein.core.writer import Writer
from serein.ingest import ob_types
from serein.recall.index import build_index
from serein.recall.service import Recall
from serein.compat.memo_store import ReminderStore
from serein.api.mcp import create_server
from serein.api.http import create_app
from serein.application import Application


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(tmp_path / 'synthetic.db', writable=True)
    initialize(settings)
    with Store(settings.database) as store:
        store.create('event_rain', 'event', 'Rain source', 'We watched the rain together.')
        store.create('scene_promise', 'scene', 'Trust', 'A lasting understanding of trust.')
        source = store.add_source('synthetic/message/1', 'I like listening to rain.')
        binding = store.bind('event_rain', source)
    return settings


def refs():
    return [{'kind': 'event', 'id': 'event_rain'}]


def create(runtime, **kwargs):
    return Feelings(runtime.database).create('I felt calm in the rain.', refs(), author='Synthetic AI', **kwargs)


def mapping(runtime):
    with Store(runtime.database, read_only=True) as store:
        source = store.read('event_rain')
    return {'old-rain': {'kind': 'event', 'id': 'event_rain', 'revision': source['revision'], 'sha256': source['body_sha256']}}


def legacy():
    return [
        {'id': 'old-feel', 'content': '  I felt calm.\n', 'metadata': {'type': 'feel', 'triggered_by': 'old-rain',
         'valence': .8, 'arousal': .2, 'manual_surface': 0, 'pinned': True, 'protected': True,
         'created': '2025-01-02T08:00:00+08:00', 'future_field': {'keep': 7}}},
        {'id': 'old-ring', 'type': 'annotation', 'kind': 'feel', 'content': 'I feel differently now.',
         'parent_id': 'old-rain', 'digested': True, 'dont_surface': True, 'manual_surface': 1},
        {'id': 'missing', 'type': 'feel', 'content': 'Original with a lost export link.'},
        *[{'id': 'plan-' + status, 'type': 'plan', 'content': 'Synthetic promise ' + status,
           'status': status, 'weight': .9, 'related_bucket': 'old-rain', 'change_log': [{'action': 'old action'}]}
          for status in ('active', 'resolved', 'abandoned')]]


def test_independent_feelings_versions_explicit_source_search_and_unchanged_source(runtime):
    with Reader(runtime.database) as reader:
        before = reader.read('event_rain')
    first = create(runtime, authored_at='2025-01-01T00:00:00+00:00')
    second = create(runtime, authored_at='2025-01-02T00:00:00+00:00')
    assert first['id'] != second['id'] and first['kind'] == 'feel'
    assert first['evidence'] == [] and first['evidence_scope'] == 'subjective_feeling_not_evidence'
    assert first['feel_sources'][0]['revision'] == 1
    with Reader(runtime.database) as reader:
        after = reader.read('event_rain')
        assert after['document'] == before['document'] and after['evidence'] == before['evidence']
        assert 'feelings' not in after and 'plans' not in after
    found = Feelings(runtime.database).search(source_id='event_rain')['items']
    assert {item['id'] for item in found} == {first['id'], second['id']}
    with Store(runtime.database) as store:
        store.revise('event_rain', expected_revision=1, title='Corrected', body_md='Later correction')
    saved = Feelings(runtime.database).read(first['id'])
    assert saved['feel_sources'][0]['summary'] == 'We watched the rain together.'
    assert saved['feel_sources'][0]['current_revision'] == 2
    assert saved['feel_sources'][0]['sha256'] == digest('We watched the rain together.')


@pytest.mark.parametrize('sources', [[], [{'kind': 'event', 'id': 'missing'}],
    [{'kind': 'narrative', 'id': 'event_rain'}], [{'kind': 'scene', 'id': 'event_rain'}]])
def test_missing_or_wrong_source_rejected_without_side_effect(runtime, sources):
    with pytest.raises(ValueError):
        Feelings(runtime.database).create('I feel something.', sources, author='Synthetic')
    with Store(runtime.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 2


def test_source_archive_deletion_and_lock_boundaries(runtime):
    obj = create(runtime, can_surface=True)
    with Store(runtime.database) as store:
        store.set_lifecycle('event_rain', 'archived')
    assert Feelings(runtime.database).read(obj['id'])['surface_state']['can_surface']
    with Store(runtime.database) as store:
        store.revise('event_rain', expected_revision=1, title='Private title', body_md='Private source', metadata={'locked': True})
    locked = Feelings(runtime.database).read(obj['id'])
    assert locked['readable'] and locked['feel_sources'][0]['status'] == 'restricted'
    assert 'We watched' not in encode(locked) and 'Private source' not in encode(locked)
    assert not locked['surface_state']['can_surface']
    with Reader(runtime.database) as reader:
        assert not reader.read('event_rain', revision=1)['readable']
    with Store(runtime.database) as store:
        store.set_lifecycle('event_rain', 'deleted')
    deleted = Feelings(runtime.database).read(obj['id'])
    assert deleted['feel_sources'][0]['status'] == 'unavailable' and deleted['readable']


def test_raw_source_requires_active_authorized_binding(runtime):
    with Store(runtime.database, read_only=True) as store:
        row = store.conn.execute('SELECT * FROM evidence_bindings').fetchone()
    reference = {'kind': 'source', 'id': row['source_id'], 'binding_id': row['id']}
    item = Feelings(runtime.database).create('I felt heard.', [reference], author='Synthetic', can_surface=True)
    assert item['feel_sources'][0]['summary'] == 'I like listening to rain.'
    with Store(runtime.database) as store:
        store.unbind(row['id'])
    assert not Feelings(runtime.database).read(item['id'])['surface_state']['can_surface']
    with pytest.raises(ValueError):
        Feelings(runtime.database).create('No backdoor.', [reference], author='Synthetic')


def test_feel_literal_search_dates_flags_and_cas(runtime):
    first = create(runtime, authored_at='2025-01-02T00:00:00+08:00')
    second = create(runtime, authored_at='2025-01-03T00:00:00+08:00')
    service = Feelings(runtime.database)
    result = service.search('calm', source_id='event_rain', date_from='2025-01-02', date_to='2025-01-02')
    assert [i['id'] for i in result['items']] == [first['id']]
    archived = service.edit(first['id'], 1, lifecycle='archived')
    assert not archived['surface_state']['can_surface']
    assert len(service.search('calm')['items']) == 1
    assert len(service.search('calm', include_archived=True)['items']) == 2
    with pytest.raises(Conflict):
        service.edit(first['id'], 1, content='Stale')
    with Writer(runtime.database) as writer, pytest.raises(ValueError):
        writer.execute('bad', 'save', {'document_id': first['id'], 'expected_revision': 2, 'kind': 'scene',
                                     'title': 'Strip role', 'body_md': 'no', 'metadata': {}})


def test_plan_lifecycle_is_explicit_and_reminder_expiry_is_independent(runtime):
    plans = Plans(runtime.database)
    first = plans.create('I will revisit this.', actor='Synthetic', related=refs())
    reminder = ReminderStore({'serein_database': str(runtime.database)})
    expired = reminder.create(title='Once', content='Optional delivery', end_at='2000-01-01', reminder_id='expired')
    linked = plans.update(first['id'], 1, actor='user', reason='Enable optional delivery', reminder_id='expired')
    reminder.archive_expired()
    assert reminder.get('expired')['status'] == 'archived'
    assert plans.read(first['id'])['status'] == 'active'
    done = plans.update(first['id'], 2, actor='user', reason='Explicitly completed', status='resolved')
    abandoned = plans.update(first['id'], 3, actor='user', reason='Correction: abandoned', status='abandoned')
    assert plans.read(first['id'], 1)['status'] == 'active'
    assert plans.read(first['id'], 3)['status'] == 'resolved'
    assert abandoned['status'] == 'abandoned' and len(abandoned['history']) == 4
    assert abandoned['original_content'] == first['content']
    with pytest.raises(ValueError):
        plans.update(first['id'], 4, actor='user', reason='', status='resolved')
    with pytest.raises(Conflict):
        plans.update(first['id'], 1, actor='user', reason='Stale', status='resolved')
    with Reader(runtime.database) as reader:
        assert 'plans' not in reader.read('event_rain')
        assert not reader.read(first['id'])['surface_state']['can_surface']
    with Store(runtime.database, read_only=True) as store:
        assert not store.read(first['id'])


def test_import_preview_missing_links_flags_roundtrip_idempotency_and_undo(runtime, tmp_path):
    Personal(runtime.database).annotate('event_rain', 'An existing annotation', 'Synthetic', 'assistant', 'old-note')
    records, ids = legacy(), mapping(runtime)
    preview = ob_types.preview(runtime.database, records, ids, namespace='synthetic-ob')
    assert preview['counts'] == {'create': 5, 'reuse': 0, 'pending_link': 1, 'review': 0}
    options = dict(namespace='synthetic-ob', batch_id='batch', expected_preview_sha256=preview['preview_sha256'], backup_path=tmp_path/'before.db')
    result = ob_types.apply(runtime.database, records, ids, **options)
    assert ob_types.apply(runtime.database, records, ids, **options) == result
    again = ob_types.preview(runtime.database, records, ids, namespace='synthetic-ob')
    assert again['counts']['reuse'] == 5
    with Reader(runtime.database) as reader:
        for item in result['created']:
            if item['kind'] == 'feel':
                feel = reader.read(item['id'])
                assert not feel['surface_state']['can_surface']
                assert feel['evidence'] == []
        assert reader.read('event_rain')['annotations'][0]['content'] == 'An existing annotation'
    plans = Plans(runtime.database).list()['items']
    assert {p['status'] for p in plans} == {'active', 'resolved', 'abandoned'}
    assert all(p['weight'] == .9 and p['reminder_id'] is None for p in plans)
    assert all(p['compatibility']['original']['change_log'] == [{'action': 'old action'}] for p in plans)
    assert all(p['authored_at'] is None and p['occurred_at'] is None for p in plans)
    with Store(runtime.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM import_records').fetchone()[0] == len(records)
        assert store.conn.execute('SELECT count(*) FROM authored_plans').fetchone()[0] == 3
    undone = ob_types.undo(runtime.database, 'batch')
    assert undone['status'] == 'undone' and ob_types.undo(runtime.database, 'batch') == undone
    with Store(runtime.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM authored_plans').fetchone()[0] == 0


def test_import_atomic_failure_and_changed_source_precondition(runtime, tmp_path, monkeypatch):
    records, ids = legacy(), mapping(runtime)
    preview = ob_types.preview(runtime.database, records, ids, namespace='synthetic')
    options = dict(namespace='synthetic', batch_id='atomic', expected_preview_sha256=preview['preview_sha256'], backup_path=tmp_path/'atomic.db')
    real = Plans.create_in_store
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic interruption after feelings')
    monkeypatch.setattr(Plans, 'create_in_store', fail)
    with pytest.raises(RuntimeError):
        ob_types.apply(runtime.database, records, ids, **options)
    with Store(runtime.database) as store:
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM import_records').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM ob_type_imports').fetchone()[0] == 0
        store.revise('event_rain', expected_revision=1, title='Correction', body_md='Changed after preview')
    monkeypatch.setattr(Plans, 'create_in_store', real)
    with pytest.raises(Conflict, match='Preview changed'):
        ob_types.apply(runtime.database, records, ids, **{**options, 'backup_path': tmp_path/'stale.db'})


def test_import_undo_refuses_to_erase_new_edits(runtime, tmp_path):
    records, ids = legacy(), mapping(runtime)
    preview = ob_types.preview(runtime.database, records, ids, namespace='synthetic')
    receipt = ob_types.apply(runtime.database, records, ids, namespace='synthetic', batch_id='undo',
        expected_preview_sha256=preview['preview_sha256'], backup_path=tmp_path/'undo.db')
    key = receipt['created'][0]['id']
    Feelings(runtime.database).edit(key, 1, content='New authored text')
    with pytest.raises(Conflict):
        ob_types.undo(runtime.database, 'undo')
    assert Feelings(runtime.database).read(key)['document']['body_md'] == 'New authored text'


def test_mcp_and_http_entry_points_and_readonly(runtime):
    server = create_server(Application(runtime))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {'write_feel', 'edit_feel', 'read_feel', 'find_feelings', 'create_plan', 'read_plan', 'list_plans', 'update_plan'} <= names
    from test_authored_mcp_parity import call
    created = call(server, 'write_feel', content='I felt calm.', sources=refs())
    assert created['memory_role'] == 'feel'
    text = call(server, 'read_memory', identifier='feel:' + created['id'])
    assert 'type: feel' in text and 'trigger: event:event_rain' in text
    with TestClient(create_app(runtime, token='synthetic-test', live=True), headers={'Authorization': 'Bearer synthetic-test'}) as client:
        result = client.post('/api/authored-types', json={'tool': 'create_plan', 'arguments': {'content': 'I will return.'}})
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'active'
    readonly = create_server(Application(replace(runtime, writable=False)))
    names = {tool.name for tool in asyncio.run(readonly.list_tools())}
    assert 'read_feel' in names and 'write_feel' not in names and 'update_plan' not in names


@pytest.mark.parametrize('mixed', [False, True])
def test_automatic_feeling_candidate_admission_cooldown_budget_and_source_dedup(runtime, tmp_path, monkeypatch, mixed):
    first = create(runtime, can_surface=True)
    off = create(runtime, can_surface=False)
    plans = Plans(runtime.database).create('Rain promise is not a fact.', actor='Synthetic')
    index = tmp_path / 'recall.db'
    build_index(runtime.database, index)
    profile = dict(model='synthetic', provider_host='fixture.invalid', query_instruction='', document_instruction='', max_chars=6000)
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (encode(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        for key in (first['id'], off['id'], 'event_rain'):
            conn.execute('INSERT INTO vectors VALUES (?,?,2)', (key, '[1,0]'))
    class Embedding:
        def __init__(self, *args, **kwargs):
            pass
        def query(self, text):
            return {'query': text, 'profile': profile, 'embedding': [1, 0]}
    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient', Embedding)
    policy = {}
    if mixed:
        policy_file = tmp_path / 'gate.json'
        policy_file.write_text('{}', encoding='utf-8')
        policy = {'routing_file': 'unused', 'germany_policy_file': str(policy_file)}
        monkeypatch.setattr('serein.recall.routing.route_query', lambda *a, **kw: {
            'route': 'present_chitchat', 'action': 'recall', 'reason': 'uncertain_route',
            'scores': [{'name': 'present_chitchat', 'action': 'skip', 'score': .47, 'threshold': .6}]})
    settings = replace(runtime, index=index, embedding={'endpoint': 'unused'}, recall={**policy, 'max_cards': 2})
    seen = []
    def rank(query, documents):
        seen.extend(documents)
        return {row['ref']: .9 for row in documents}
    engine = Recall(settings, reranker=rank)
    result = engine.run('How did I feel about the rain?', method='semantic', min_cosine=.5)
    assert 'scene:' + first['id'] in result['selected_refs'], result
    assert 'scene:' + off['id'] not in result['selected_refs']
    assert plans['id'] not in encode(result) and len(result['cards']) <= 2
    assert '当时的主观感受' in result['context'] and 'revision=1' in result['context']
    # Source is returned as its own card, so the feel does not repeat its prose.
    assert result['context'].count('We watched the rain together.') == 1
    assert not result['injected']
    cooled = engine.run('How did I feel about the rain?', method='semantic', min_cosine=.5, delivered_ids=result['selected_refs'])
    assert not cooled['selected_refs']
    with Store(runtime.database) as store:
        store.set_lifecycle('event_rain', 'deleted')
    assert not engine.run('How did I feel about the rain?', method='semantic', min_cosine=.5)['selected_refs']


def test_reminder_count_exhaustion_does_not_settle_plan(runtime):
    reminder = ReminderStore({'serein_database': str(runtime.database)})
    row = reminder.create(title='One delivery', content='Synthetic reminder', max_injections=1)
    plans = Plans(runtime.database)
    plan = plans.create('Keep this promise.', actor='Synthetic')
    plans.update(plan['id'], 1, actor='Synthetic', reason='Attach', reminder_id=row['id'])
    reminder.mark_reminded(row['id'], round_id=1)
    assert reminder.get(row['id'])['status'] == 'archived'
    assert plans.read(plan['id'])['status'] == 'active'


def test_restricted_unknown_genre_time_and_flags_require_review(runtime):
    records = [
        {'id': 'locked', 'type': 'feel', 'content': 'Sealed', 'source_bucket': 'old-rain', 'locked': True},
        {'id': 'deleted', 'type': 'plan', 'content': 'Removed', 'deleted_at': '2025-01-01'},
        {'id': 'whisper', 'type': 'feel', 'content': 'Not a feeling source', 'tags': ['whisper']},
        {'id': 'time', 'type': 'feel', 'content': 'No timezone', 'source_bucket': 'old-rain', 'created': '2025-01-01'},
        {'id': 'flag', 'type': 'feel', 'content': 'Ambiguous flag', 'source_bucket': 'old-rain', 'dont_surface': 'false'}]
    result = ob_types.preview(runtime.database, records, mapping(runtime), namespace='synthetic')
    assert result['counts']['review'] == 5 and result['counts']['create'] == 0


def test_export_preserves_old_bytes_emotion_unknown_fields_and_versions(runtime, tmp_path):
    rows, ids = legacy(), mapping(runtime)
    review = ob_types.preview(runtime.database, rows, ids, namespace='export')
    ob_types.apply(runtime.database, rows, ids, namespace='export', batch_id='export',
                   expected_preview_sha256=review['preview_sha256'], backup_path=tmp_path/'backup.db')
    archive = ob_types.export_types(runtime.database)
    old = next(item for item in archive['feelings'] if item['document']['metadata']['compatibility']['old_id'] == 'old-feel')
    assert old['document']['body_md'] == '  I felt calm.\n'
    meta = old['document']['metadata']
    assert (meta['valence'], meta['arousal']) == (.8, .2)
    assert meta['compatibility']['original']['metadata']['future_field'] == {'keep': 7}
    assert meta['pinned'] and meta['protected'] and not old['document']['manual_surface']
    assert len(archive['plans']) == 3 and len(archive['plan_versions']) == 3


def test_schema_nine_read_only_and_explicit_upgrade_keep_existing_values(tmp_path):
    database = tmp_path/'v9.db'
    with sqlite3.connect(database) as conn:
        from pathlib import Path
        root = Path(__file__).parents[1]/'src/serein/core'
        for suffix in ('', *('_v'+str(i) for i in range(2, 10))):
            conn.executescript((root/('schema'+suffix+'.sql')).read_text(encoding='utf-8'))
    with Store(database, read_only=True) as store:
        assert store.conn.execute('PRAGMA user_version').fetchone()[0] == 9
    with Store(database) as store:
        assert store.conn.execute('PRAGMA user_version').fetchone()[0] == 10
    assert Plans(database).list()['items'] == []


def test_embedded_ring_adapter_and_explicit_duplicate_identity(runtime):
    buckets = [legacy()[0], {'id': 'old-rain', 'content': 'Source retained separately', 'metadata': {'comments': [
        {'id': 'ring', 'kind': 'feel', 'content': '  I felt calm.\n', 'original_feel_id': 'old-feel',
         'original_feel_created': '2025-01-02T08:00:00+08:00'},
        {'id': 'ordinary-note', 'kind': 'comment', 'content': 'Keep as annotation'}]}}]
    records = ob_types.records_from_buckets(buckets)
    assert len(records) == 2 and records[1]['parent_id'] == 'old-rain'
    report = ob_types.preview(runtime.database, records, mapping(runtime), namespace='synthetic')
    assert report['counts']['review'] == 2 and len(report['potential_duplicates']) == 1
    assert report['counts']['create'] == 0


def test_stale_source_hash_and_malformed_emotion_never_create(runtime):
    with pytest.raises(Conflict):
        Feelings(runtime.database).create('Stale', [{**refs()[0], 'sha256': 'wrong'}], author='Synthetic')
    for value in (float('nan'), -1, 2, True):
        with pytest.raises(ValueError):
            create(runtime, valence=value)
    assert Feelings(runtime.database).search()['items'] == []


def test_local_cli_preview_apply_export_and_undo(runtime, tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = Path(__file__).parents[1]/'scripts/ob_types.py'
    input_file = tmp_path/'reviewed.json'
    input_file.write_text(encode({'namespace': 'cli-fixture', 'records': legacy(), 'identity_map': mapping(runtime)}), encoding='utf-8')
    def run(action, name, *options):
        output = tmp_path/name
        result = subprocess.run([sys.executable, str(script), action, '--database', str(runtime.database),
            '--output', str(output), *options], capture_output=True, text=True, encoding='utf-8', env=os.environ)
        assert result.returncode == 0, result.stderr
        return json.loads(output.read_text(encoding='utf-8'))
    review = run('preview', 'preview.json', '--input', str(input_file))
    receipt = run('apply', 'receipt.json', '--input', str(input_file), '--batch-id', 'cli',
                  '--expected-preview-sha256', review['preview_sha256'], '--backup', str(tmp_path/'before.db'))
    assert receipt['status'] == 'applied'
    exported = run('export', 'export.json')
    assert len(exported['feelings']) == 2 and len(exported['plans']) == 3
    assert run('undo', 'undo.json', '--batch-id', 'cli')['status'] == 'undone'
