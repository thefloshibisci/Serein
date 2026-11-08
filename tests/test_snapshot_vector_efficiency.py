"""Synthetic Snapshot payload and recall parity checks; no model requests."""

from collections import Counter
import json
import math
import sqlite3

import pytest

from serein.config import Settings
from serein.core.feelings import Feelings
from serein.core.store import Store
from serein.recall import typed_surface
from serein.recall.index import Search, build_index, content_stamp
from serein.recall.passages import ensure_tables
from serein.recall.policy import RecallPolicy
from serein.recall.query import Query


PROFILE = dict(model='synthetic', provider_host='fixture.invalid', query_instruction='',
               document_instruction='', max_chars=6000)


class ObservedConnection:
    """Record actual SQLite rows crossing into Python, not only query text."""

    def __init__(self, connection):
        self.connection = connection
        self.reads = []
        self.queries = []

    def execute(self, statement, parameters=()):
        cursor = self.connection.execute(statement, parameters)
        table = next((name for name in ('vectors', 'passages')
                      if 'FROM ' + name in statement and 'embedding' in statement), None)
        if table is None:
            return cursor
        self.queries.append((table, statement, tuple(parameters)))

        def rows():
            for row in cursor:
                key = row['id'] if table == 'vectors' else row['document_id']
                self.reads.append((table, key, row['embedding']))
                yield row

        return rows()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def create_document(store, key, **kwargs):
    return store.create(key, kwargs.pop('kind', 'event'), 'Synthetic ' + key,
                        'Synthetic memory body for ' + key, **kwargs)


def build_runtime(tmp_path, vectors, *, passage_rows=()):
    settings = Settings(tmp_path / 'memory.db', tmp_path / 'index.db')
    build_index(settings.database, settings.index)
    with Store(settings.database, read_only=True) as store:
        stamps = {key: content_stamp(store.read(key)) for key in vectors if store.read(key)}
    with sqlite3.connect(settings.index) as connection:
        connection.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(PROFILE),))
        connection.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        connection.executemany('INSERT INTO vectors VALUES (?,?,2)', vectors.items())
        ensure_tables(connection)
        for key, ordinal, vector, stamp in passage_rows:
            connection.execute('INSERT INTO passages VALUES (?,?,?,?,?,?,?,?,?)',
                               (key, ordinal, stamps.get(key, '') if stamp is None else stamp,
                                ordinal, ordinal + 1, 'synthetic passage', vector, 2, 'synthetic'))
    return settings


def snapshot(settings, *, query=None, policy=None, cutoff=-1, use_passages=True):
    query = query or Query('synthetic query', user_utterance=True)
    embedding = dict(query=query.text, profile=PROFILE, embedding=[1, 0])
    with Search(settings.database, settings.index) as search:
        observed = ObservedConnection(search.conn)
        search.conn = observed
        result = typed_surface.Snapshot(search, query, policy or RecallPolicy(),
                                        embedding, cutoff, use_passages, settings)
    return result, observed


def full_scan_rows(connection, statement, _identifiers):
    """The vector queries used before owner filtering, for result parity."""
    if 'FROM vectors' in statement:
        return connection.execute('SELECT id,embedding FROM vectors')
    return connection.execute('SELECT * FROM passages WHERE embedding IS NOT NULL')


def test_filtered_owner_vectors_never_cross_into_python(tmp_path, monkeypatch):
    with Store(tmp_path / 'memory.db') as store, store.transaction():
        for key in ('eligible', 'manual_off', 'unreviewed', 'archived', 'deleted', 'stale',
                    'excluded', 'domain', 'replaced', 'covered', 'cover', 'source_locked', 'source_deleted'):
            create_document(store, key, kind='scene' if key == 'cover' else 'event',
                            metadata={'domain': 'blocked'} if key == 'domain' else {})
        source = store.add_source('synthetic/shared', 'Synthetic evidence snapshot')
        store.bind('covered', source)
        store.bind('cover', source)
        store.set_manual_surface('cover', False)
        store.conn.execute("INSERT INTO event_replacements VALUES ('replaced','eligible','synthetic','{}')")
    feelings = Feelings(tmp_path / 'memory.db')
    locked_feel = feelings.create('Synthetic feeling with a locked source.',
                                 [{'kind': 'event', 'id': 'source_locked'}], author='Synthetic', can_surface=True)['id']
    deleted_feel = feelings.create('Synthetic feeling with a deleted source.',
                                  [{'kind': 'event', 'id': 'source_deleted'}], author='Synthetic', can_surface=True)['id']
    with Store(tmp_path / 'memory.db') as store:
        keys = [row[0] for row in store.conn.execute('SELECT id FROM documents')]
    vectors = {key: '[9,9]' if key != 'eligible' else '[1,0]' for key in keys}
    settings = build_runtime(tmp_path, vectors,
                             passage_rows=[(key, 0, vectors[key], None) for key in keys])
    with Store(settings.database) as store:
        store.set_manual_surface('manual_off', False)
        store.set_manual_surface('unreviewed', None)
        store.set_manual_surface('source_locked', False)
        store.set_lifecycle('archived', 'archived')
        store.set_lifecycle('deleted', 'deleted')
        store.set_lifecycle('source_deleted', 'deleted')
        for key in ('stale', 'source_locked'):
            store.revise(key, expected_revision=1, title='Changed', body_md='Changed synthetic body',
                         metadata={'locked': True} if key == 'source_locked' else {})
    decoded = []
    original_decode = typed_surface._decode_vector

    def observe_decode(raw):
        decoded.append(raw)
        return original_decode(raw)

    monkeypatch.setattr(typed_surface, '_decode_vector', observe_decode)
    result, observed = snapshot(settings, query=Query('synthetic query', exclude_ids=('excluded',)),
                                policy=RecallPolicy(domains={'blocked': 'excluded'}))
    assert set(result.catalog) == {'eligible'}
    assert {key for _table, key, _raw in observed.reads} == {'eligible'}
    assert Counter(table for table, _key, _raw in observed.reads) == {'vectors': 1, 'passages': 1}
    assert decoded == ['[1,0]', '[1,0]']
    assert locked_feel not in result.catalog and deleted_feel not in result.catalog
    assert result.suppressed['feel_surface_policy'] == 2


def test_more_than_500_owners_remain_complete_and_match_full_scan(tmp_path, monkeypatch):
    keys = [f'event_{number:04}' for number in range(1003)]
    with Store(tmp_path / 'memory.db') as store, store.transaction():
        for key in keys:
            create_document(store, key)
        create_document(store, 'off', manual_surface=False)
    vectors = {key: json.dumps([1 - number / 2000, number / 2000]) for number, key in enumerate(keys)}
    vectors['off'] = '[99,99]'
    settings = build_runtime(tmp_path, vectors,
                             passage_rows=[(key, 0, vector, None) for key, vector in vectors.items()])
    optimized, observed = snapshot(settings)
    for table in ('vectors', 'passages'):
        batches = [parameters for name, _sql, parameters in observed.queries if name == table]
        assert len(batches) >= 3
        assert max(map(len, batches)) <= 500
        assert sum(map(len, batches)) == len(keys)
        assert {key for name, key, _vector in observed.reads if name == table} == set(keys)
    monkeypatch.setattr(typed_surface, '_rows_for_ids', full_scan_rows)
    reference, reference_reads = snapshot(settings)
    assert 'off' in {key for _name, key, _vector in reference_reads.reads}
    assert optimized.whole == reference.whole and optimized.passages == reference.passages
    assert optimized.suppressed == reference.suppressed
    assert optimized.whole_search('event', None, 1005) == reference.whole_search('event', None, 1005)
    arguments = dict(top_k=1005, owner_kinds=('event',), passages_per_owner=2)
    assert optimized.search_by_embedding([1, 0], **arguments) == reference.search_by_embedding([1, 0], **arguments)
    found = {'entity_scope': {}, 'lanes': {}}
    query = Query('synthetic query')
    assert typed_surface.mixed_candidates(optimized, found, {}, query) == typed_surface.mixed_candidates(reference, found, {}, query)


@pytest.mark.parametrize('use_passages', [False, True])
def test_empty_catalog_does_not_read_any_vector_payload(tmp_path, use_passages):
    with Store(tmp_path / 'memory.db') as store:
        create_document(store, 'hidden', manual_surface=False)
    settings = build_runtime(tmp_path, {'hidden': '[not a vector]'},
                             passage_rows=[('hidden', 0, '[not a vector]', None)])
    result, observed = snapshot(settings, use_passages=use_passages)
    assert result.catalog == {} and result.whole == {} and result.passages == {}
    assert observed.reads == observed.queries == []


def test_invalid_dimension_and_stale_passage_behavior_matches_full_scan(tmp_path, monkeypatch):
    vectors = {'valid': '[1,0]', 'json': 'not-json', 'object': '{}',
               'boolean': '[true,0]', 'nan': '[NaN,0]', 'dimension': '[1,0,0]'}
    with Store(tmp_path / 'memory.db') as store, store.transaction():
        for key in vectors:
            create_document(store, key)
    passages = [(key, 0, vector, None) for key, vector in vectors.items()]
    passages += [('valid', 1, 'stale-vector-must-not-decode', 'outdated-stamp'),
                 ('valid', 2, None, None)]
    settings = build_runtime(tmp_path, vectors, passage_rows=passages)
    seen = []
    original_decode = typed_surface._decode_vector

    def observe_decode(raw):
        seen.append(raw)
        return original_decode(raw)

    monkeypatch.setattr(typed_surface, '_decode_vector', observe_decode)
    optimized, observed = snapshot(settings)
    assert set(optimized.whole) == set(optimized.passages) == {'valid'}
    assert optimized.suppressed['invalid_whole_vector'] == 4
    assert optimized.suppressed['invalid_passage_vector'] == 4
    assert 'stale-vector-must-not-decode' not in seen and None not in seen
    assert not any(raw is None for _name, _key, raw in observed.reads)
    monkeypatch.setattr(typed_surface, '_rows_for_ids', full_scan_rows)
    reference, _observed = snapshot(settings)
    assert optimized.whole == reference.whole and optimized.passages == reference.passages
    assert optimized.suppressed == reference.suppressed


@pytest.mark.parametrize('cutoff', [.4999, .5, .5001, .5499, .55, .5501, .6499, .65, .6501])
def test_four_decimal_scores_sorting_and_cutoff_match_full_scan(tmp_path, monkeypatch, cutoff):
    values = [.499949999, .49995, .499950001, .5, .500049999, .50005, .500050001,
              .549949999, .54995, .55005, .649949999, .64995, .65005, .9, .9]
    vectors = {f'owner_{number:02}': json.dumps([value, math.sqrt(1 - value * value)])
               for number, value in enumerate(values)}
    with Store(tmp_path / 'memory.db') as store, store.transaction():
        for number, key in enumerate(vectors):
            create_document(store, key, kind='scene' if number % 2 else 'event')
    settings = build_runtime(tmp_path, vectors,
                             passage_rows=[(key, ordinal, vector, None)
                                           for key, vector in reversed(list(vectors.items())) for ordinal in (2, 0, 1)])
    optimized, _ = snapshot(settings, cutoff=cutoff)
    monkeypatch.setattr(typed_surface, '_rows_for_ids', full_scan_rows)
    reference, _ = snapshot(settings, cutoff=cutoff)
    assert optimized.whole == reference.whole
    assert optimized.passages == reference.passages
    for kind in ('event', 'scene'):
        assert optimized.whole_search(kind, None, 30) == reference.whole_search(kind, None, 30)
    arguments = dict(top_k=30, owner_kinds=('event', 'scene'), passages_per_owner=2)
    assert optimized.search_by_embedding([1, 0], **arguments) == reference.search_by_embedding([1, 0], **arguments)
    query = Query('synthetic query')
    found = {'entity_scope': {}, 'lanes': {}}
    ranked = typed_surface.mixed_candidates(optimized, found, {}, query)
    reference_ranked = typed_surface.mixed_candidates(reference, found, {}, query)
    assert ranked == reference_ranked
    for threshold in (.5, .55, .65):
        assert typed_surface.select_candidate_pool(optimized, found, ranked, [], query, body_threshold=threshold) == typed_surface.select_candidate_pool(reference, found, reference_ranked, [], query, body_threshold=threshold)


def test_owner_eligibility_is_rechecked_between_snapshots(tmp_path):
    with Store(tmp_path / 'memory.db') as store:
        create_document(store, 'owner')
    settings = build_runtime(tmp_path, {'owner': '[1,0]'})
    first, _ = snapshot(settings)
    assert set(first.whole) == {'owner'}
    with Store(settings.database) as store:
        store.set_manual_surface('owner', False)
    hidden, hidden_reads = snapshot(settings)
    assert hidden.whole == {} and hidden_reads.reads == []
    with Store(settings.database) as store:
        store.set_manual_surface('owner', True)
    restored, _ = snapshot(settings)
    assert restored.whole == first.whole
    with Store(settings.database) as store:
        store.revise('owner', expected_revision=1, title='Revised', body_md='Revised body')
    stale, stale_reads = snapshot(settings)
    assert stale.whole == {} and stale_reads.reads == []


def test_feel_source_access_is_rechecked_without_caching_decisions(tmp_path):
    with Store(tmp_path / 'memory.db') as store:
        create_document(store, 'source', manual_surface=False)
    feeling = Feelings(tmp_path / 'memory.db').create(
        'Synthetic subjective feeling.', [{'kind': 'event', 'id': 'source'}],
        author='Synthetic', can_surface=True)['id']
    settings = build_runtime(tmp_path, {feeling: '[1,0]', 'source': '[99,99]'})
    first, _ = snapshot(settings)
    assert set(first.whole) == {feeling}
    with Store(settings.database) as store:
        store.revise('source', expected_revision=1, title='Restricted', body_md='Restricted source',
                     metadata={'locked': True})
    locked, locked_reads = snapshot(settings)
    assert locked.whole == {} and locked_reads.reads == []
    with Store(settings.database) as store:
        store.revise('source', expected_revision=2, title='Available', body_md='Available source', metadata={})
    unlocked, _ = snapshot(settings)
    assert unlocked.whole == first.whole
    with Store(settings.database) as store:
        store.set_lifecycle('source', 'deleted')
    deleted, deleted_reads = snapshot(settings)
    assert deleted.whole == {} and deleted_reads.reads == []
