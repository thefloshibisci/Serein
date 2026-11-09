"""Canonical payload reads and exact digest caches never cache visibility."""

import json
import sqlite3

from serein.core.feelings import Feelings
from serein.core.store import Store, encode
from serein.recall import typed_surface
from serein.recall.index import Search
from serein.recall.policy import RecallPolicy
from serein.recall.query import Query
from test_snapshot_vector_efficiency import PROFILE, build_runtime, create_document


class ObservedCursor:
    def __init__(self, cursor, reads):
        self.cursor, self.reads = cursor, reads
        self.payload = {'body_md', 'metadata_json'} <= {item[0] for item in cursor.description or ()}

    def _record(self, row):
        if row is not None and self.payload:
            self.reads.append((row['id'], row['body_md'], row['metadata_json']))
        return row

    def __iter__(self):
        return (self._record(row) for row in self.cursor)

    def fetchall(self):
        return [self._record(row) for row in self.cursor.fetchall()]

    def fetchone(self):
        return self._record(self.cursor.fetchone())

    def __getattr__(self, key):
        return getattr(self.cursor, key)


class ObservedCanonical:
    def __init__(self, connection):
        self.connection, self.reads, self.queries = connection, [], []

    def execute(self, statement, parameters=()):
        self.queries.append((statement, tuple(parameters)))
        return ObservedCursor(self.connection.execute(statement, parameters), self.reads)

    def __getattr__(self, key):
        return getattr(self.connection, key)


def take_snapshot(settings, *, query=None):
    query = query or Query('synthetic query', user_utterance=True)
    with Search(settings.database, settings.index) as search:
        observed = ObservedCanonical(search.reader.store.conn)
        search.reader.store.conn = observed
        result = typed_surface.Snapshot(search, query, RecallPolicy(),
            dict(query=query.text, profile=PROFILE, embedding=[1, 0]), -1, True, settings)
    return result, observed


def public_snapshot(result):
    return {key: getattr(result, key) for key in
            ('objects', 'catalog', 'whole', 'passages', 'suppressed', 'cue_allowed', 'lexical_allowed')}


def full_payloads(connection, _identifiers):
    return connection.execute(
        "SELECT d.*,r.number AS body_revision,r.title,r.body_md,r.body_sha256,r.metadata_json "
        "FROM documents d JOIN revisions r ON r.document_id=d.id AND r.number=d.revision "
        "WHERE d.kind IN ('scene','event')")


def test_inactive_quiet_and_unindexed_bodies_are_not_materialized(tmp_path, monkeypatch):
    with Store(tmp_path / 'memory.db') as store:
        for key in ('eligible', 'manual_off', 'unreviewed', 'archived', 'deleted', 'superseded', 'covered'):
            create_document(store, key, metadata={'synthetic_payload': key * 1000})
        create_document(store, 'quiet_cover', kind='scene', manual_surface=False)
        create_document(store, 'unindexed_cover', kind='scene')
        create_document(store, 'unindexed', metadata={'synthetic_payload': 'must not load'})
        source = store.add_source('synthetic/shared', 'Synthetic shared source')
        for key in ('covered', 'quiet_cover', 'unindexed_cover'):
            store.bind(key, source)
    vectors = {key: '[1,0]' for key in
               ('eligible', 'manual_off', 'unreviewed', 'archived', 'deleted', 'superseded', 'covered', 'quiet_cover')}
    settings = build_runtime(tmp_path, vectors)
    with sqlite3.connect(settings.index) as connection:
        connection.execute("DELETE FROM documents WHERE id IN ('unindexed', 'unindexed_cover')")
        connection.execute("INSERT INTO documents VALUES ('missing','event','synthetic-missing')")
    with Store(settings.database) as store:
        store.set_manual_surface('manual_off', False)
        store.set_manual_surface('unreviewed', None)
        for state in ('archived', 'deleted', 'superseded'):
            store.set_lifecycle(state, state)
    optimized, observed = take_snapshot(settings)
    assert {key for key, _body, _meta in observed.reads} == {'eligible', 'covered'}
    assert set(optimized.catalog) == {'eligible'}
    assert optimized.suppressed == {'manual_surface_not_enabled': 3, 'lifecycle_not_active': 3,
                                    'covered_by_scene': 1, 'missing_or_deleted': 1}
    monkeypatch.setattr(typed_surface, '_document_payloads', full_payloads)
    reference, reference_reads = take_snapshot(settings)
    assert 'unindexed' in {key for key, _body, _meta in reference_reads.reads}
    assert public_snapshot(optimized) == public_snapshot(reference)


def test_canonical_payload_batches_remain_complete_over_sqlite_parameter_limits(tmp_path):
    keys = [f'event_{number:04}' for number in range(1003)]
    with Store(tmp_path / 'memory.db') as store, store.transaction():
        for key in keys:
            create_document(store, key)
    settings = build_runtime(tmp_path, {key: '[1,0]' for key in keys})
    result, observed = take_snapshot(settings)
    batches = [parameters for statement, parameters in observed.queries
               if 'WHERE d.id IN (' in statement]
    assert len(batches) == 3 and max(map(len, batches)) <= 400
    assert set(result.catalog) == set(keys)
    assert {key for key, _body, _meta in observed.reads} == set(keys)


def test_digest_cache_uses_exact_body_title_and_raw_metadata_at_same_revision(tmp_path):
    with Store(tmp_path / 'memory.db') as store:
        create_document(store, 'owner', metadata={'custom': 'original'})
    settings = build_runtime(tmp_path, {'owner': '[1,0]'})
    typed_surface._document_stamp.cache_clear()
    initial, _ = take_snapshot(settings)
    warm, _ = take_snapshot(settings)
    assert initial.whole == warm.whole == {'owner': 1.0}
    assert typed_surface._document_stamp.cache_info().hits >= 1
    for column, changed, original in (
            ('title', 'Different title', 'Synthetic owner'),
            ('body_md', 'Different body', 'Synthetic memory body for owner'),
            ('metadata_json', encode({'custom': 'different'}), encode({'custom': 'original'}))):
        before = typed_surface._document_stamp.cache_info().misses
        with sqlite3.connect(settings.database) as connection:
            connection.execute(f'UPDATE revisions SET {column}=? WHERE document_id=? AND number=1',
                               (changed, 'owner'))
        stale, _ = take_snapshot(settings)
        assert stale.catalog == {} and stale.suppressed['stale_content_rebuild_required'] == 1
        assert typed_surface._document_stamp.cache_info().misses == before + 1
        with sqlite3.connect(settings.database) as connection:
            connection.execute(f'UPDATE revisions SET {column}=? WHERE document_id=? AND number=1',
                               (original, 'owner'))
        restored, _ = take_snapshot(settings)
        assert restored.whole == initial.whole
    before = typed_surface._document_stamp.cache_info().misses
    with sqlite3.connect(settings.database) as connection:
        connection.execute('UPDATE revisions SET metadata_json=? WHERE document_id=? AND number=1',
                           (json.dumps({'custom': 'original'}, indent=2), 'owner'))
    reformatted, _ = take_snapshot(settings)
    assert reformatted.whole == initial.whole
    assert typed_surface._document_stamp.cache_info().misses == before + 1


def test_digest_cache_does_not_reuse_same_identifier_in_another_database(tmp_path):
    typed_surface._document_stamp.cache_clear()
    settings_by_database = []
    for key, metadata in [('first', {'custom': 'one'}), ('second', {'custom': 'two'})]:
        root = tmp_path / key
        root.mkdir()
        with Store(root / 'memory.db') as store:
            create_document(store, 'owner', metadata=metadata)
        settings_by_database.append(build_runtime(root, {'owner': '[1,0]'}))
    first, _ = take_snapshot(settings_by_database[0])
    second, _ = take_snapshot(settings_by_database[1])
    assert first.whole == second.whole == {'owner': 1.0}
    assert first.objects['owner']['document']['metadata'] != second.objects['owner']['document']['metadata']
    assert typed_surface._document_stamp.cache_info().misses == 2
    assert typed_surface._document_stamp.cache_info().maxsize == 4096


def test_cached_digest_does_not_cache_live_flags_or_feel_source_access(tmp_path):
    with Store(tmp_path / 'memory.db') as store:
        create_document(store, 'source', manual_surface=False)
    feel = Feelings(tmp_path / 'memory.db').create(
        'Synthetic feeling', [{'kind': 'event', 'id': 'source'}],
        author='Synthetic', can_surface=True)['id']
    settings = build_runtime(tmp_path, {feel: '[1,0]'})
    first, _ = take_snapshot(settings)
    warm, _ = take_snapshot(settings)
    assert first.whole == warm.whole == {feel: 1.0}
    with Store(settings.database) as store:
        store.set_manual_surface(feel, False)
    quiet, _ = take_snapshot(settings)
    assert quiet.whole == {} and quiet.suppressed['manual_surface_not_enabled'] == 2
    with Store(settings.database) as store:
        store.set_manual_surface(feel, True)
        store.revise('source', expected_revision=1, title='Locked', body_md='Locked source', metadata={'locked': True})
    locked, _ = take_snapshot(settings)
    assert locked.whole == {} and locked.suppressed['feel_surface_policy'] == 1
    with Store(settings.database) as store:
        store.revise('source', expected_revision=2, title='Available', body_md='Available source', metadata={})
    restored, _ = take_snapshot(settings)
    assert restored.whole == first.whole
    with Store(settings.database) as store:
        store.set_lifecycle(feel, 'archived')
    archived, _ = take_snapshot(settings)
    assert archived.whole == {} and archived.suppressed['lifecycle_not_active'] == 1
