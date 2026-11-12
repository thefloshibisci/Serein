"""Offline synthetic ZIPs prove preservation and archive read restrictions."""
import base64
from copy import deepcopy
import io
import json
import sqlite3
import zipfile

import pytest

from serein.api.typed_authored import tools_for
from serein.config import Settings
from serein.core.store import Store, Conflict, digest, encode
from serein.ingest import ob_archive, ob_backup, ob_types, ob_self


def bucket(key, body='  unchanged\r\n\r\n', **metadata):
    import yaml
    meta = {'id': key, 'type': 'dynamic', 'created': '2026-09-03T17:01:00', **metadata}
    return ('---\r\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=True).replace('\n', '\r\n')
            + '---\r\n' + body).encode()


def module_db(module, *, enabled=1):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE module_state(singleton,enabled,scope_json)')
    conn.execute('INSERT INTO module_state VALUES (1,?,?)', (enabled, '{"subject_user_id":"synthetic"}'))
    conn.execute('CREATE TABLE claims(id,scope_key,payload_json)')
    conn.execute('INSERT INTO claims VALUES (?,?,?)', ('claim-a', 'scope-a', '{"content":"PRIVATE CLAIM","lifecycle":"candidate"}'))
    if module == 'you':
        conn.execute('CREATE TABLE projections(scope_key,payload_json)')
    else:
        conn.execute('CREATE TABLE persons(id,scope_key,payload_json)')
        conn.execute('INSERT INTO persons VALUES (?,?,?)', ('person-a', 'scope-a', '{"names":["PRIVATE PERSON"]}'))
    raw = conn.serialize();conn.close()
    return raw


def backup(tmp_path, rows=None, extra=None, manifest=True):
    files = rows or {'buckets/dynamic/source.md': bucket('source'),
                     'buckets/feel/feel.md': bucket('feel-a', type='feel', triggered_by='source'),
                     'buckets/archive/erased.md': bucket('erased', type='feel', tombstone=True, erasure_mode='metadata_only'),
                     'buckets/dynamic/self.md': bucket('self-a', type='i', source_tool='I'),
                     'buckets/letters/locked.md': bucket('locked', 'SECRET LETTER', type='letter', lock_type='permanent', locked_by='human'),
                     'buckets/plans/plan.md': bucket('plan-a', type='plan', status='abandoned', change_log=[{'action': 'old action'}])}
    files = {**files, 'export_meta.json': b'{"version":"synthetic","exported_at":"2026-11-08T17:01:00"}',
             'you/you.sqlite3': module_db('you'), 'them/them.sqlite3': module_db('them'), **(extra or {})}
    if manifest:
        files['backup_manifest.json'] = encode({'file_count': len(files), 'files': [
            {'path': name, 'size': len(raw), 'sha256': digest(raw)} for name, raw in files.items()]}).encode()
    target = tmp_path/'source.zip'
    with zipfile.ZipFile(target, 'w') as stream:
        for name, raw in files.items():
            stream.writestr(name, raw)
    return target, files


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path/'target.db'
    with Store(path):
        pass
    return path


def test_zip_preserves_original_bytes_unknown_metadata_and_time_basis(tmp_path):
    path, files = backup(tmp_path, rows={'buckets/dynamic/a.md': bucket('a', extra={'keep': [1, '未知']})})
    before = path.read_bytes()
    envelope = ob_backup.read_backup(path, namespace='synthetic')
    record = envelope['records'][0]
    assert base64.b64decode(record['original_file_b64']) == files['buckets/dynamic/a.md']
    assert record['content'] == '  unchanged\r\n\r\n' and record['body_sha256'] == digest(record['content'])
    assert record['metadata']['extra'] == {'keep': [1, '未知']}
    assert record['authored_time']['value'] is None and record['authored_time']['basis'] == 'missing_source_timezone'
    assert base64.b64decode(envelope['backup_b64']) == before == path.read_bytes()
    ob_backup.validate_envelope(envelope)


def test_timezone_evidence_attaches_offset_without_changing_clock_or_explicit_offset(tmp_path):
    path, _ = backup(tmp_path, rows={
        'buckets/dynamic/a.md': bucket('a'),
        'buckets/dynamic/b.md': bucket('b', created='2026-09-03T01:02:03+08:00')})
    policy = {'zone': 'UTC', 'evidence': 'Synthetic old writer used UTC-naive timestamps'}
    envelope = ob_backup.read_backup(path, namespace='synthetic', timezone_policy=policy)
    a, b = envelope['records']
    assert a['authored_time']['value'] == '2026-09-03T17:01:00+00:00'
    assert a['metadata']['created'] == '2026-09-03T17:01:00'
    assert b['authored_time']['value'] == '2026-09-03T01:02:03+08:00'
    assert b['authored_time']['basis'] == 'original_explicit_offset'
    with pytest.raises(ValueError, match='evidence'):
        ob_backup.read_backup(path, namespace='synthetic', timezone_policy={'zone': 'UTC'})


@pytest.mark.parametrize('member', ['../outside.md', '/absolute.md', 'C:/private.md', 'buckets\\escape.md'])
def test_unsafe_paths_rejected_without_extracting(tmp_path, member):
    path, _ = backup(tmp_path, extra={member: b'not executed'}, manifest=False)
    if '\\' in member:
        # ZipInfo normalizes os.sep on Windows; exercise an actual hostile wire name.
        normalized = member.replace('\\', '/').encode()
        path.write_bytes(path.read_bytes().replace(normalized, member.encode()))
    with pytest.raises(ValueError, match='unsafe'):
        ob_backup.read_backup(path, namespace='synthetic')
    assert not (tmp_path/'outside.md').exists()


def test_manifest_tampering_and_duplicate_ids_are_rejected(tmp_path):
    path, _ = backup(tmp_path, extra={'backup_manifest.json': b'{"file_count":0,"files":[]}'}, manifest=False)
    with pytest.raises(ValueError, match='manifest'):
        ob_backup.read_backup(path, namespace='synthetic')
    path.unlink()
    path, _ = backup(tmp_path, rows={'buckets/dynamic/a.md': bucket('same'), 'buckets/archive/b.md': bucket('same')})
    with pytest.raises(ValueError, match='Duplicate old IDs'):
        ob_backup.read_backup(path, namespace='synthetic')


def test_archive_transaction_retry_keeps_all_source_bytes_but_no_documents(runtime, tmp_path):
    path, _ = backup(tmp_path)
    envelope = ob_backup.read_backup(path, namespace='synthetic')
    report = ob_archive.preview(runtime, envelope)
    assert report['genres']['erased'] == 1 and report['records'] == 6
    receipt = ob_archive.apply(runtime, envelope, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    assert receipt['status'] == 'applied'
    with Store(runtime, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
        original = store.conn.execute("SELECT content FROM import_records WHERE path='backup.zip'").fetchone()[0]
        assert original == path.read_bytes()
    second = ob_archive.preview(runtime, envelope)
    assert ob_archive.apply(runtime, envelope, expected_preview_sha256=second['preview_sha256'], backup_path=tmp_path/'unused.db')['status'] == 'reused'
    assert not (tmp_path/'unused.db').exists()
    assert not ob_archive.read_record(runtime, 'synthetic', 'erased')['readable']
    assert 'SECRET' not in encode(ob_archive.read_record(runtime, 'synthetic', 'locked'))


def test_failed_catalog_insert_rolls_back_original_zip(runtime, tmp_path, monkeypatch):
    path, _ = backup(tmp_path)
    envelope = ob_backup.read_backup(path, namespace='synthetic')
    report = ob_archive.preview(runtime, envelope)
    original = Store.save_import_record
    def fail(store, origin, name, raw):
        if name == 'catalog.json':
            raise RuntimeError('synthetic crash')
        return original(store, origin, name, raw)
    monkeypatch.setattr(Store, 'save_import_record', fail)
    with pytest.raises(RuntimeError, match='synthetic crash'):
        ob_archive.apply(runtime, envelope, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    with Store(runtime, read_only=True) as store:
        assert not store.conn.execute('SELECT 1 FROM import_records').fetchone()


def test_public_tools_cannot_change_identity_or_bypass_scope_or_locks(runtime, tmp_path):
    path, _ = backup(tmp_path)
    envelope = ob_backup.read_backup(path, namespace='synthetic')
    report = ob_archive.preview(runtime, envelope)
    ob_archive.apply(runtime, envelope, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    tools = tools_for(Settings(runtime, writable=False))
    directory = tools['list_ob_archive']('synthetic', limit=100)
    assert 'SECRET' not in encode(directory) and 'PRIVATE CLAIM' not in encode(directory)
    assert not tools['read_ob_archive']('synthetic', 'you:claim:claim-a')['readable']
    assert not tools['read_ob_archive']('synthetic', 'them:person:person-a')['readable']
    from serein.api.typed_authored import mcp_tools_for
    archive = mcp_tools_for(Settings(runtime, writable=False))['ob_archive']
    assert archive('synthetic', limit=100) == directory
    assert not archive('synthetic', record_id='you:claim:claim-a')['readable']
    assert not archive('synthetic', record_id='them:person:person-a')['readable']
    assert not archive('synthetic', record_id='locked')['readable']
    with pytest.raises(TypeError):
        tools['read_ob_archive']('synthetic', 'locked', caller_side='human')


def test_reviewed_scope_binding_only_opens_that_scope_and_never_enables_recall(runtime, tmp_path):
    path, _ = backup(tmp_path)
    envelope = ob_backup.read_backup(path, namespace='synthetic')
    envelope['scope_bindings'] = {'you': [{'source_scope_key': 'scope-a', 'target': 'instance_ai', 'evidence': 'Synthetic audited same scope'}]}
    envelope['envelope_sha256'] = ob_backup.envelope_digest(envelope)
    report = ob_archive.preview(runtime, envelope)
    ob_archive.apply(runtime, envelope, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    allowed = ob_archive.read_record(runtime, 'synthetic', 'you:claim:claim-a')
    assert allowed['readable'] and 'PRIVATE CLAIM' in encode(allowed) and not allowed['recall_enabled']
    assert not ob_archive.read_record(runtime, 'synthetic', 'them:claim:claim-a')['readable']


def test_modified_body_even_with_new_review_hash_fails_original_integrity(runtime, tmp_path):
    path, _ = backup(tmp_path)
    envelope = ob_backup.read_backup(path, namespace='synthetic')
    envelope['records'][0]['content'] = 'silently rewritten'
    envelope['envelope_sha256'] = ob_backup.envelope_digest(envelope)
    with pytest.raises(ValueError, match='immutable'):
        ob_archive.preview(runtime, envelope)


def test_typed_selection_retains_i_and_never_promotes_erasure_or_ordinary_memory(tmp_path):
    path, _ = backup(tmp_path)
    envelope = ob_backup.read_backup(path, namespace='synthetic')
    selected = ob_backup.typed_records(envelope)
    assert {row['id'] for row in selected} == {'feel-a', 'self-a', 'plan-a'}
    assert ob_self.is_self_record(next(row for row in selected if row['id'] == 'self-a'))


def test_metadata_only_tombstone_never_resurrects_typed_record(runtime):
    row = {'id': 'erased', 'content': 'Never revive', 'metadata': {'type': 'feel', 'tombstone': True}}
    report = ob_types.preview(runtime, [row], {}, namespace='synthetic')
    assert report['items'][0]['action'] == 'review' and report['counts']['create'] == 0


@pytest.mark.parametrize(('metadata', 'status'), [
    ({'lock_type': 'timed', 'locked_by': 'human', 'unlock_date': '2999-01-01T00:00:00+00:00'}, 'locked'),
    ({'lock_type': 'timed', 'locked_by': 'human', 'unlock_date': '2000-01-01T00:00:00+00:00'}, 'available'),
    ({'lock_type': 'unknown'}, 'unresolved_lock'),
    ({'unlock_at': '2999-01-01T00:00:00+00:00'}, 'locked'),
    ({'unlock_at': '2999-01-01T00:00:00'}, 'locked'),
    ({'visibility': 'private'}, 'locked'),
])
def test_archive_lock_dates_and_unknown_visibility_fail_closed(metadata, status):
    assert ob_archive._status({'genre': 'letter', 'metadata': metadata}) == status
