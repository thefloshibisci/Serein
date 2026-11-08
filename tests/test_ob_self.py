"""Synthetic self migration; no private instance, old library, or model calls."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from test_ob_types import runtime
from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.application import Application
from serein.compat.window_shadows import WindowShadows
from serein.compat.work_reads import read_works
from serein.core.reader import Reader
from serein.core.store import Store, Conflict, digest, encode
from serein.deployment import read_settings, save_settings
from serein.ingest import ob_types, ob_self


def record(key='old-self', **meta):
    return {'id': key, 'content': '  I choose to remain curious.\n### reflection\nKeep every word.\n',
            'metadata': {'type': 'permanent', 'tags': ['自我'], 'pinned': True, 'protected': True,
                         'manual_surface': 1, 'future': {'exact': ['preserved']}, **meta}}


def apply(runtime, tmp_path, rows, choices=None, batch='self', review=None):
    choices = choices or {}
    review = review or ob_types.preview(runtime.database, rows, {}, namespace='synthetic-self', self_mappings=choices)
    return ob_types.apply(runtime.database, rows, {}, namespace='synthetic-self', self_mappings=choices,
        batch_id=batch, expected_preview_sha256=review['preview_sha256'], backup_path=tmp_path/(batch+'.db'))


def scene_ref(runtime):
    with Store(runtime.database, read_only=True) as store:
        doc = store.read('scene_promise')
    return {'kind': 'scene', 'id': doc['id'], 'revision': doc['revision'], 'sha256': doc['body_sha256']}


def test_adapter_and_unmapped_records_preserve_original_for_later_decision(runtime, tmp_path):
    rows = ob_types.records_from_buckets([record(), {'id': 'ordinary', 'content': 'Talking about 自我'}])
    assert rows == [record()]
    before = runtime.database.read_bytes()
    review = ob_types.preview(runtime.database, rows, {}, namespace='synthetic-self')
    assert review['counts']['review'] == 1 and review['items'][0]['original'] == rows[0]
    assert runtime.database.read_bytes() == before
    receipt = apply(runtime, tmp_path, rows)
    assert receipt['created'] == []
    exported = ob_types.export_types(runtime.database)
    assert exported['original_records'][0]['content'] == rows[0]
    assert not exported['self_content']['scenes'] and not exported['self_content']['shadows']
    # A later reviewed choice uses exactly the same retained export, not guessed prose.
    assert len(apply(runtime, tmp_path, rows, {'old-self': {'target': 'scene'}}, batch='decided')['created']) == 1


@pytest.mark.parametrize('archived', [False, True])
def test_scene_exact_body_versions_flags_and_explicit_continuity(runtime, tmp_path, archived):
    rows = [record(archived=archived, dont_surface=True, digested=True, created='2025-02-01T08:00:00+08:00')]
    choices = {'old-self': {'target': 'scene'}}
    receipt = apply(runtime, tmp_path, rows, choices)
    key = receipt['created'][0]['id']
    with Reader(runtime.database) as reader:
        result = reader.read(key)
        doc = result['document']
        assert doc['body_md'] == rows[0]['content']
        assert doc['metadata']['compatibility']['original'] == rows[0]
        assert doc['metadata']['authored_at'] == rows[0]['metadata']['created']
        assert doc['metadata']['occurred_at'] is None
        assert result['evidence'] == [] and not result['surface_state']['can_surface']
        assert doc['lifecycle'] == ('archived' if archived else 'active')
        assert not doc['manual_surface'] and doc['metadata']['legacy_flags']['manual_surface']
    save_settings(runtime.database, {'features': {'resume': True, 'window_shadows': True},
        'resume': {'selected_memories': True, 'selected_ids': [key], 'latest_shadow': False,
                   'recent_events': False, 'favorite_scenes': False, 'pending_originals': False}})
    result = Application(runtime).contributions.tools['resume']('synthetic-next')
    assert [item['id'] for item in result['items']] == ([] if archived else [key])
    if not archived:
        WindowShadows(runtime.database).write('new', 'New understanding', self_view='I still choose curiosity.', scene_ids=[key])
        assert WindowShadows(runtime.database).read()['scene_ids'] == [key]
    archive = ob_types.export_types(runtime.database)['self_content']['scenes'][0]
    assert archive['versions'][0]['body_md'] == rows[0]['content']


@pytest.mark.parametrize('existing', [False, True])
def test_historical_shadow_cannot_replace_current_self_or_latest_reads(runtime, tmp_path, existing):
    save_settings(runtime.database, {'features': {'window_shadows': True, 'resume': True}})
    shadows = WindowShadows(runtime.database)
    if existing:
        shadows.write('current', 'Current', self_view='My current understanding')
    before = read_settings(runtime.database)
    rows = [record(created='2099-01-01T00:00:00Z')]
    choice = {'old-self': {'target': 'historical_shadow', 'scene_refs': [scene_ref(runtime)]}}
    receipt = apply(runtime, tmp_path, rows, choice)
    key = receipt['created'][0]['id']
    assert read_settings(runtime.database) == before
    assert shadows.read().get('window_id') == ('current' if existing else None)
    assert shadows.read(key)['content'] == rows[0]['content']
    assert rows[0]['content'] not in read_works(runtime.database, 'shadow')
    resumed = Application(runtime).contributions.tools['resume']('new')
    assert key not in [item['id'] for item in resumed['items']]
    with Reader(runtime.database) as reader:
        historical = reader.read('shadow:'+key)
        assert historical['readable'] and historical['document']['body_md'] == rows[0]['content']
        meta = historical['document']['metadata']
        assert not meta['continuity_eligible'] and meta['self_context_refs'] == [scene_ref(runtime)]
    client = TestClient(create_app(runtime, token='test', live=True), headers={'Authorization': 'Bearer test'})
    assert all(w['window_id'] != key for w in client.get('/api/window-shadows').json()['windows'])
    assert any(w['window_id'] == key for w in client.get('/api/window-shadows?include_historical=true').json()['windows'])
    assert client.get('/api/window-shadows', params={'window_id': key}).json()['window']['content'] == rows[0]['content']
    server = create_server(Application(runtime))
    from test_authored_mcp_parity import call
    text = call(server, 'read_memory', identifier='shadow:'+key)
    assert 'Keep every word.' in text
    with Store(runtime.database) as store:
        store.revise('scene_promise', expected_revision=1, title='Secret source title',
                     body_md='Secret source body', metadata={'locked': True})
    response = client.get('/api/window-shadows', params={'window_id': key}).json()['window']
    assert response['scenes'] == [] and 'Secret source title' not in encode(response)
    with Store(runtime.database) as store:
        store.record_deletion(key, '2026-11-08T00:00:00Z', {})
    assert shadows.read(key)['status'] == 'not_found'
    with Reader(runtime.database) as reader:
        assert not reader.read('shadow:'+key)['readable']


def test_source_change_after_preview_rolls_back_all_objects(runtime, tmp_path):
    rows = [record('scene'), record('shadow')]
    choices = {'scene': {'target': 'scene'}, 'shadow': {'target': 'historical_shadow', 'scene_refs': [scene_ref(runtime)]}}
    preview = ob_types.preview(runtime.database, rows, {}, namespace='synthetic-self', self_mappings=choices)
    with Store(runtime.database) as store:
        store.revise('scene_promise', expected_revision=1, title='Changed', body_md='Changed source')
    with pytest.raises(Conflict, match='Preview changed'):
        apply(runtime, tmp_path, rows, choices, review=preview)
    with Store(runtime.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM ob_type_imports').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM import_records').fetchone()[0] == 0
    with sqlite3.connect(tmp_path/'self.db') as backup:
        assert backup.execute('SELECT count(*) FROM documents').fetchone()[0] == 2


def test_mid_batch_failure_has_no_partial_import(runtime, tmp_path, monkeypatch):
    rows = [record('one'), record('two')]
    choices = {r['id']: {'target': 'scene'} for r in rows}
    native = ob_self.create_in_store
    def fail(store, item):
        if item['old_id'] == 'two':
            raise RuntimeError('synthetic interruption')
        return native(store, item)
    monkeypatch.setattr(ob_self, 'create_in_store', fail)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        apply(runtime, tmp_path, rows, choices)
    assert ob_types.export_types(runtime.database)['original_records'] == []
    assert ob_types.export_types(runtime.database)['self_content'] == {'scenes': [], 'shadows': []}


@pytest.mark.parametrize('target', ['scene', 'historical_shadow'])
def test_idempotence_conflicts_targeted_undo_and_original_preservation(runtime, tmp_path, target):
    rows = [record()]
    choices = {'old-self': {'target': target}}
    preview = ob_types.preview(runtime.database, rows, {}, namespace='synthetic-self', self_mappings=choices)
    receipt = apply(runtime, tmp_path, rows, choices, review=preview)
    assert apply(runtime, tmp_path, rows, choices, review=preview) == receipt
    assert ob_types.preview(runtime.database, rows, {}, namespace='synthetic-self', self_mappings=choices)['counts']['reuse'] == 1
    other = {'old-self': {'target': 'scene' if target == 'historical_shadow' else 'historical_shadow'}}
    with pytest.raises(Conflict, match='different content or mapping'):
        ob_types.preview(runtime.database, rows, {}, namespace='synthetic-self', self_mappings=other)
    with pytest.raises(Conflict):
        ob_types.preview(runtime.database, [dict(rows[0], content='Changed')], {}, namespace='synthetic-self', self_mappings=choices)
    assert ob_types.undo(runtime.database, 'self')['status'] == 'undone'
    assert ob_types.undo(runtime.database, 'self')['status'] == 'undone'
    archive = ob_types.export_types(runtime.database)
    assert archive['self_content'] == {'scenes': [], 'shadows': []}
    assert archive['original_records'][0]['content'] == rows[0]
    with Store(runtime.database, read_only=True) as store:
        assert store.read('scene_promise')['body_md'] == 'A lasting understanding of trust.'


@pytest.mark.parametrize('reference', ['shadow', 'selection', 'relation', 'edit'])
def test_undo_refuses_changed_or_newly_referenced_scene(runtime, tmp_path, reference):
    receipt = apply(runtime, tmp_path, [record()], {'old-self': {'target': 'scene'}})
    key = receipt['created'][0]['id']
    if reference == 'shadow':
        WindowShadows(runtime.database).write('next', 'Next', self_view='Continuing', scene_ids=[key])
    elif reference == 'selection':
        save_settings(runtime.database, {'resume': {'selected_ids': [key]}})
    else:
        with Store(runtime.database) as store:
            if reference == 'edit':
                store.revise(key, expected_revision=1, title='Edited', body_md='New understanding')
            else:
                store.conn.execute('INSERT INTO scene_relations VALUES (?,?,?,?,?,?,?)',
                                   ('relation', 'synthetic', key, 'scene_promise', 'active', 1, '{}'))
    with pytest.raises(Conflict):
        ob_types.undo(runtime.database, 'self')
    with Store(runtime.database, read_only=True) as store:
        assert store.read(key) and json.loads(store.conn.execute('SELECT receipt_json FROM ob_type_imports').fetchone()[0])['status'] == 'applied'


@pytest.mark.parametrize('flags', [{'locked': True}, {'deleted': True}, {'unlock_at': '2099-01-01T00:00:00Z'},
    {'created': '2025-01-01'}, {'pinned': 'true'}, {'self_anchor': 'false'}, {'lifecycle': 'unknown'}, {'type': 'plan'}])
def test_restricted_or_ambiguous_self_content_only_retained_for_review(runtime, tmp_path, flags):
    rows = [record(**flags)]
    receipt = apply(runtime, tmp_path, rows, {'old-self': {'target': 'scene'}})
    assert receipt['preview']['counts']['review'] == 1 and receipt['created'] == []
    assert ob_types.export_types(runtime.database)['original_records'][0]['content'] == rows[0]


def test_outer_fields_cannot_unseal_nested_legacy_metadata(runtime, tmp_path):
    rows = [dict(record(locked=True), locked=False)]
    assert apply(runtime, tmp_path, rows, {'old-self': {'target': 'scene'}})['created'] == []


@pytest.mark.parametrize('target', ['scene', 'historical_shadow'])
def test_second_receipt_dependency_and_deletion_block_undo_and_resurrection(runtime, tmp_path, target):
    rows = [record()]
    choices = {'old-self': {'target': target}}
    first = apply(runtime, tmp_path, rows, choices)
    second = apply(runtime, tmp_path, rows, choices, batch='second')
    assert not second['created'] and second['preview']['counts']['reuse'] == 1
    with pytest.raises(Conflict, match='Another import receipt'):
        ob_types.undo(runtime.database, 'self')
    ob_types.undo(runtime.database, 'second')
    key = first['created'][0]['id']
    with Store(runtime.database) as store:
        store.record_deletion(key, '2026-11-08T00:00:00Z', {})
    with pytest.raises(Conflict):
        ob_types.undo(runtime.database, 'self')
    with pytest.raises(Conflict, match='deleted'):
        ob_types.preview(runtime.database, rows, {}, namespace='synthetic-self', self_mappings=choices)


def test_cli_self_mapping_preview_apply_read_export_undo(runtime, tmp_path):
    input_file = tmp_path/'reviewed.json'
    input_file.write_text(encode({'namespace': 'synthetic-self', 'records': [record()], 'identity_map': {},
        'self_mappings': {'old-self': {'target': 'historical_shadow'}}}), encoding='utf-8')
    def run(action, *args):
        output = tmp_path/(action+'.json')
        result = subprocess.run([sys.executable, str(Path(__file__).parents[1]/'scripts/ob_types.py'), action,
            '--database', str(runtime.database), '--output', str(output), *args],
            capture_output=True, text=True, encoding='utf-8', env=os.environ)
        assert result.returncode == 0, result.stderr
        return json.loads(output.read_text(encoding='utf-8'))
    preview = run('preview', '--input', str(input_file))
    receipt = run('apply', '--input', str(input_file), '--batch-id', 'cli-self',
        '--expected-preview-sha256', preview['preview_sha256'], '--backup', str(tmp_path/'before.db'))
    assert receipt['created'][0]['kind'] == 'shadow'
    exported = run('export')
    assert exported['self_content']['shadows'][0]['body_md'] == record()['content']
    assert json.loads(exported['self_content']['shadows'][0]['metadata_json'])['authored_at'] is None
    assert run('undo', '--batch-id', 'cli-self')['status'] == 'undone'
