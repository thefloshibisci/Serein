"""Synthetic counterpart to full backup migration, with atomicity/idempotency."""
from copy import deepcopy
from pathlib import Path

import pytest

from serein.core.store import Store, Conflict, digest, encode
from serein.ingest import ob_backup, ob_migration, ob_archive, ob_types
from test_ob_backup import backup, bucket


def envelope(tmp_path):
    path, _ = backup(tmp_path)
    value = ob_backup.read_backup(path, namespace='synthetic', timezone_policy={
        'zone': 'UTC', 'evidence': 'Synthetic old writer used UTC-naive timestamps'})
    value['self_mappings'] = {'self-a': {'target': 'scene'}}
    value['envelope_sha256'] = ob_backup.envelope_digest(value)
    return value


def test_migration_reuses_exact_old_identity_and_body_then_creates_linked_feel_plan(tmp_path):
    database = tmp_path/'target.db'
    data = envelope(tmp_path)
    with Store(database) as store:
        old = store.create('scene_existing', 'scene', 'Already migrated', data['records'][0]['content'],
                           metadata={'legacy_id': 'source'}, manual_surface=True)
    report = ob_migration.preview(database, data)
    assert report['counts']['sources'] == {'reuse': 1, 'create': 1}
    assert report['counts']['created_kinds'] == {'scene': 1, 'feel': 1, 'plan': 1}
    assert report['counts']['erased_originals'] == 1
    assert report['events_created'] == report['auto_surface_enabled'] == 0
    with Store(database, read_only=True) as store:
        assert store.read('scene_existing') == old and store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 1
    receipt = ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    assert receipt['status'] == 'applied'
    with Store(database, read_only=True) as store:
        assert store.read('scene_existing') == old
        created = [store.read(row['id']) for row in receipt['created'] if row['kind'] != 'plan']
        assert all(row['manual_surface'] in (0, None) for row in created)
        feeling = next(row for row in created if row['metadata'].get('memory_role') == 'feel')
        assert feeling['metadata']['feel_sources'][0]['id'] == 'scene_existing'
    assert ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'unused.db') == receipt
    assert not (tmp_path/'unused.db').exists()
    assert not ob_archive.read_record(database, 'synthetic', 'erased')['readable']


def test_same_old_id_different_body_and_missing_source_remain_review_not_merge(tmp_path):
    database = tmp_path/'target.db'
    data = envelope(tmp_path)
    with Store(database) as store:
        store.create('scene_existing', 'scene', 'Conflicting', 'Different body', metadata={'legacy_id': 'source'})
    report = ob_migration.preview(database, data)
    assert report['sources'][0]['reason'] == 'exact_id_body_changed'
    feeling = next(item for item in report['typed'] if item['old_id'] == 'feel-a')
    assert feeling['action'] == 'pending_link'
    with Store(database, read_only=True) as store:
        assert store.read('scene_existing')['body_md'] == 'Different body'


def test_mid_apply_failure_rolls_back_sources_types_and_original_archive(tmp_path, monkeypatch):
    database = tmp_path/'target.db'
    with Store(database):
        pass
    data = envelope(tmp_path)
    report = ob_migration.preview(database, data)
    original = ob_migration._execute
    def fail(store, envelope):
        result = original(store, envelope)
        if Path(store.path) == database:
            raise RuntimeError('Synthetic transaction failure')
        return result
    monkeypatch.setattr(ob_migration, '_execute', fail)
    with pytest.raises(RuntimeError, match='Synthetic transaction failure'):
        ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    with Store(database, read_only=True) as store:
        for table in ('documents', 'revisions', 'authored_plans', 'authored_plan_revisions', 'import_records'):
            assert store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0


def test_source_change_after_preview_rejected_before_mutation(tmp_path):
    database = tmp_path/'target.db'
    data = envelope(tmp_path)
    with Store(database) as store:
        store.create('scene_existing', 'scene', 'Already migrated', data['records'][0]['content'], metadata={'legacy_id': 'source'})
    report = ob_migration.preview(database, data)
    with Store(database) as store:
        store.revise('scene_existing', expected_revision=1, title='Later edit', body_md='Changed')
    with pytest.raises(Conflict, match='preview changed'):
        ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    assert not (tmp_path/'before.db').exists()


def test_guarded_undo_removes_only_own_new_objects_and_retains_archive(tmp_path):
    database = tmp_path/'target.db'
    with Store(database) as store:
        original = store.create('scene_unrelated', 'scene', 'Unrelated', 'Original')
    data = envelope(tmp_path)
    report = ob_migration.preview(database, data)
    ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    undone = ob_migration.undo(database, data['namespace'])
    assert undone['status'] == 'undone' and undone['original_archive_retained']
    with Store(database, read_only=True) as store:
        assert store.read('scene_unrelated') == original
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 1
        assert store.conn.execute('SELECT count(*) FROM authored_plans').fetchone()[0] == 0
    assert ob_archive.read_record(database, 'synthetic', 'source')['readable']
    assert ob_migration.undo(database, data['namespace']) == undone
    with pytest.raises(Conflict, match='undone'):
        ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'unused.db')


@pytest.mark.parametrize('later', ['edit', 'reference', 'personal_simulation', 'personal_review'])
def test_guarded_undo_preserves_later_edits_or_external_references(tmp_path, later):
    database = tmp_path/'target.db'
    with Store(database):
        pass
    data = envelope(tmp_path)
    report = ob_migration.preview(database, data)
    receipt = ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    key = next(item['id'] for item in receipt['created'] if item['kind'] == 'scene')
    with Store(database) as store:
        if later == 'edit':
            store.revise(key, expected_revision=1, title='Later', body_md='A later edit')
        elif later == 'reference':
            store.create('scene_new_reader', 'scene', 'Later reference', 'Keep it', metadata={'linked_source': key})
        else:
            from serein.core.personal import Personal
            if later == 'personal_simulation':
                Personal.save_in_store(store, 'recall_simulation', 'later-label',
                    {'query': 'Synthetic future query', 'expectedAction': 'recall', 'expectedMemoryIds': [key]})
            else:
                Personal.save_in_store(store, 'recall_review', 'later-label', {'candidateReviews': {key: 'relevant'}})
    with pytest.raises(Conflict):
        ob_migration.undo(database, data['namespace'])
    with Store(database, read_only=True) as store:
        assert store.read(key) is not None
        assert len(list(store.conn.execute('SELECT id FROM authored_plans'))) == 1


@pytest.mark.parametrize('kind', ['feel', 'plan'])
def test_locked_typed_records_never_bypass_archive_permissions(tmp_path, kind):
    path, _ = backup(tmp_path, rows={'buckets/'+kind+'/locked.md': bucket('locked', 'SECRET', type=kind,
                       lock_type='permanent', locked_by='human', triggered_by='source')})
    data = ob_backup.read_backup(path, namespace='locked')
    database = tmp_path/'target.db'
    with Store(database):
        pass
    report = ob_migration.preview(database, data)
    assert report['counts']['created_kinds'] == {}
    assert report['typed'][0]['action'] == 'review'
    assert report['typed'][0]['reason'] == 'restricted_or_deleted_legacy_record'


@pytest.mark.parametrize('dependent', ['new_evidence_binding', 'derived_index_foreign_key'])
def test_guarded_undo_rolls_back_whole_batch_when_later_dependent_rows_exist(tmp_path, dependent):
    database = tmp_path/'target.db'
    with Store(database):
        pass
    data = envelope(tmp_path)
    report = ob_migration.preview(database, data)
    receipt = ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    key = next(item['id'] for item in receipt['created'] if item['kind'] == 'scene')
    with Store(database) as store:
        if dependent == 'new_evidence_binding':
            source = store.add_source('later-original', 'A later preserved original')
            store.bind(key, source, binding_id='later-binding')
        else:
            # A future derived index must fail closed even when its table is not
            # listed in the authored-reference guard.
            store.conn.execute('CREATE TABLE derived_index_owners(document_id TEXT REFERENCES documents(id))')
            store.conn.execute('INSERT INTO derived_index_owners VALUES (?)', (key,))
        before_documents = [dict(row) for row in store.conn.execute('SELECT * FROM documents ORDER BY id')]
        before_plans = [dict(row) for row in store.conn.execute('SELECT * FROM authored_plans ORDER BY id')]
    with pytest.raises(Conflict, match='dependent rows'):
        ob_migration.undo(database, data['namespace'])
    with Store(database, read_only=True) as store:
        assert [dict(row) for row in store.conn.execute('SELECT * FROM documents ORDER BY id')] == before_documents
        assert [dict(row) for row in store.conn.execute('SELECT * FROM authored_plans ORDER BY id')] == before_plans
        assert store.read(key) is not None
        if dependent == 'new_evidence_binding':
            assert store.conn.execute('SELECT document_id FROM evidence_bindings WHERE id=?', ('later-binding',)).fetchone()[0] == key
        else:
            assert store.conn.execute('SELECT document_id FROM derived_index_owners').fetchone()[0] == key
        assert not store.conn.execute("SELECT 1 FROM import_records WHERE path LIKE 'undo:%'").fetchone()
    assert ob_archive.read_record(database, data['namespace'], 'source')['readable']


@pytest.mark.parametrize('later', ['locked', 'deleted'])
@pytest.mark.parametrize('reuse', [False, True])
def test_original_archive_respects_migrated_sources_current_permissions(tmp_path, later, reuse):
    database = tmp_path/'target.db'
    data = envelope(tmp_path)
    with Store(database) as store:
        if reuse:
            store.create('scene_existing', 'scene', 'Already migrated', data['records'][0]['content'],
                         metadata={'legacy_id': 'source'})
    report = ob_migration.preview(database, data)
    ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    key = next(item['id'] for item in report['sources'] if item['old_id'] == 'source')
    with Store(database) as store:
        if later == 'deleted':
            store.record_deletion(key, '2026-11-08T00:00:00Z', {})
        else:
            doc = store.read(key)
            store.revise(key, expected_revision=doc['revision'], title=doc['title'], body_md=doc['body_md'],
                         metadata={**doc['metadata'], 'locked': True})
    value = ob_archive.read_record(database, data['namespace'], 'source')
    assert not value['readable'] and 'content' not in value
    assert value['status'] in ('restricted', 'unavailable')


@pytest.mark.parametrize('later', ['types', 'migration'])
def test_guarded_undo_preserves_objects_reused_by_other_import_receipts(tmp_path, later):
    database = tmp_path/'target.db'
    data = envelope(tmp_path)
    with Store(database):
        pass
    report = ob_migration.preview(database, data)
    receipt = ob_migration.apply(database, data, expected_preview_sha256=report['preview_sha256'], backup_path=tmp_path/'before.db')
    if later == 'types':
        rows = [row for row in ob_backup.typed_records(data) if row['genre'] == 'feel']
        reviewed = ob_types.preview(database, rows, report['identity_map'], namespace=data['namespace'])
        second = ob_types.apply(database, rows, report['identity_map'], namespace=data['namespace'],
            batch_id='later-types', expected_preview_sha256=reviewed['preview_sha256'], backup_path=tmp_path/'before-second.db')
        assert second['created'] == [] and second['preview']['counts']['reuse'] == 1
    else:
        original = next(row for row in data['records'] if row['id'] == 'source')
        path, _ = backup(tmp_path, rows={original['backup_member']: bucket('source')})
        second_data = ob_backup.read_backup(path, namespace='later-source-only', timezone_policy=data['timezone_policy'])
        reviewed = ob_migration.preview(database, second_data)
        second = ob_migration.apply(database, second_data, expected_preview_sha256=reviewed['preview_sha256'],
                                   backup_path=tmp_path/'before-second.db')
        assert second['created'] == [] and second['counts']['sources'] == {'reuse': 1}
    with pytest.raises(Conflict, match='Another .*receipt'):
        ob_migration.undo(database, data['namespace'])
    with Store(database, read_only=True) as store:
        assert all(store.read(item['id']) is not None for item in receipt['created'] if item['kind'] != 'plan')
        assert not store.conn.execute("SELECT 1 FROM import_records WHERE path LIKE 'undo:%'").fetchone()
    if later == 'types':
        ob_types.undo(database, 'later-types')
    else:
        ob_migration.undo(database, 'later-source-only')
    assert ob_migration.undo(database, data['namespace'])['status'] == 'undone'
