"""Recall work stays bounded without caching stale visibility or scope results."""

import time

from serein.core import Store
from serein.core.reader import Reader
from serein.recall.rendering import _arcs
from serein.recall.typed_surface import scope_members
from test_diary_import import diary_snapshot
from test_reader import read_database
from test_personal_recall import birthday_recall


def paged_scope_members(reader):
    """Previous implementation, for semantic and query-count comparison."""
    result = {}
    for row in reader.store.conn.execute("SELECT id FROM documents WHERE kind='narrative' AND lifecycle='active'"):
        doc = reader.store.read(row['id'])
        key = doc['metadata'].get('legacy_registry', doc['metadata']).get('arc_key')
        if not key:
            continue
        refs, offset = set(), 0
        while True:
            page = reader.materials(row['id'], offset=offset, limit=100)
            refs.update((m['kind'], m['id']) for m in page['items'] if m['selection'] == 'selected'
                        and m['kind'] in ('event', 'scene') and m['object']['readable'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        result[key] = refs
    return result


def test_scope_members_match_reader_without_reading_notebook_bodies(read_database, monkeypatch):
    with Reader(read_database) as reader:
        expected = paged_scope_members(reader)
        original = reader.read

        def canonical_only(identifier, **kwargs):
            assert kwargs.get('kind') in ('event', 'scene')
            return original(identifier, **kwargs)

        monkeypatch.setattr(reader, 'read', canonical_only)
        assert scope_members(reader) == expected == {'arc_a': {('scene', 'scene_a'), ('event', 'event_a')}}
        with Store(read_database) as store:
            store.set_lifecycle('scene_a', 'deleted')
        assert scope_members(reader) == {'arc_a': {('event', 'event_a')}}


def test_shared_scope_members_are_read_once_and_pages_are_not_rebuilt(tmp_path):
    database = tmp_path / 'scope.db'
    with Store(database) as store:
        for i in range(105):
            store.create(f'scene_{i}', 'scene', 'A scene', 'Memory body')
        for arc in ('a', 'b'):
            store.create(arc, 'narrative', arc, 'Story', metadata={'arc_key': arc})
            store.conn.executemany("INSERT INTO narrative_materials VALUES (?,1,'fixture','scene',?,'linked','{}')",
                                   [(arc, f'scene_{i}') for i in range(105)])
    measurements = []
    with Reader(database) as reader:
        for implementation in (paged_scope_members, scope_members):
            statements = []
            reader.store.conn.set_trace_callback(statements.append)
            started = time.perf_counter()
            result = implementation(reader)
            measurements.append((result, len(statements), time.perf_counter() - started))
        reader.store.conn.set_trace_callback(None)
    before, after = measurements
    assert before[0] == after[0]
    assert after[1] < before[1] * .6
    print(f'\nscope: SQL {before[1]} -> {after[1]}; seconds {before[2]:.4f} -> {after[2]:.4f}')


def test_arc_menu_skips_unrelated_material_reads_and_keeps_output(read_database, monkeypatch):
    with Store(read_database) as store:
        store.create('unrelated', 'narrative', 'Unrelated', 'Story', metadata={'arc_key': 'other'})
        store.conn.execute("INSERT INTO narrative_materials VALUES ('unrelated',1,'fixture','upload','upload_a','linked','{}')")
    with Reader(read_database) as reader:
        expected = _arcs(reader, [{'id': 'scene_a'}])
        original = reader.materials
        calls = []

        def materials(identifier, **kwargs):
            calls.append(identifier)
            return original(identifier, **kwargs)

        monkeypatch.setattr(reader, 'materials', materials)
        assert _arcs(reader, [{'id': 'scene_a', 'kind': 'scene'}]) == expected
        assert calls == ['narrative_a']
        # Appended membership remains eligible even without an authored reference.
        with Store(read_database) as store:
            store.conn.execute("INSERT INTO event_arc_links VALUES ('other','event_a','fixture','{}')")
        calls.clear()
        by_owner, menus = _arcs(reader, [{'id': 'event_a', 'kind': 'event'}])
        assert {row['arc_key'] for row in by_owner['event_a']} == {'arc_a', 'other'}
        assert 'other' in menus and 'unrelated' in calls


def test_automatic_recall_ranks_once_and_queries_entities_once(birthday_recall, monkeypatch):
    from serein.recall import typed_surface
    from serein.recall.germany.memory_recall import typed_candidate_shadow
    from serein.recall.germany.memory_recall.observed_entities import ObservedEntityShadowIndex

    engine, _, scored = birthday_recall
    freshness_calls, entity_calls = [], []
    rank = typed_surface.rerank_lane_with_freshness
    entities = ObservedEntityShadowIndex.owner_query_matches

    def rank_once(rows, **kwargs):
        freshness_calls.append(True)
        return rank(rows, **kwargs)

    def query_entities(self, query, **kwargs):
        entity_calls.append(kwargs)
        return entities(self, query, **kwargs)

    def unused_pool(*args, **kwargs):
        raise AssertionError('Automatic recall must not build the retired type-quota pool')

    monkeypatch.setattr(typed_surface, 'rerank_lane_with_freshness', rank_once)
    monkeypatch.setattr(typed_candidate_shadow, 'balanced_typed_pool', unused_pool)
    monkeypatch.setattr(ObservedEntityShadowIndex, 'owner_query_matches', query_entities)
    result = engine.run('还记得你的生日吗？', method='semantic', min_cosine=.5, user_utterance=True)
    assert result['selected_refs'] == ['scene:assistant_birthday']
    assert len(scored) == len(freshness_calls) == 1
    assert len(entity_calls) == 1
    assert entity_calls[0]['limit'] == 100


def test_scope_rejection_does_not_query_candidate_entities(birthday_recall, monkeypatch):
    from serein.recall.germany.memory_recall.observed_entities import ObservedEntityShadowIndex

    engine, _, scored = birthday_recall

    def unexpected(*args, **kwargs):
        raise AssertionError('A blocked scope must not query candidate entities')

    monkeypatch.setattr(ObservedEntityShadowIndex, 'owner_query_matches', unexpected)
    result = engine.run('还记得吗？', method='semantic', min_cosine=.5, user_utterance=True)
    assert result['status'] == 'skipped' and scored == []
