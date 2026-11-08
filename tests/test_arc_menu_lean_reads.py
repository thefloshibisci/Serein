"""Arc menus omit unused enrichment while explicit reads keep their contracts."""

from copy import deepcopy
import json

import pytest

from serein.core import Store
from serein.core.feelings import Feelings
from serein.core.personal import Personal
from serein.core.plans import Plans
from serein.core.reader import Reader
from serein.core.store import digest, encode
from serein.ingest.legacy_diary import import_diaries
from serein.recall.rendering import _arcs, read_arc_picks, render
from test_diary_import import diary_snapshot
from test_reader import read_database


RELATED_FIELDS = {'feelings', 'plans', 'annotations', 'annotations_scope'}


def without_enrichment(page):
    result = deepcopy(page)
    for item in result['items']:
        if item['object']:
            for key in RELATED_FIELDS:
                item['object'].pop(key, None)
    return result


class FullAssociationsReader(Reader):
    """Retain the former menu's full reads for an output comparison."""

    def read(self, identifier, **options):
        return super().read(identifier, **{**options, 'with_related': True})

    def materials(self, identifier, **options):
        return super().materials(identifier, **{**options, 'with_related': True})


@pytest.fixture
def menu_database(tmp_path):
    database = tmp_path / 'arc-menu.db'
    with Store(database) as store:
        store.create('narrative_menu', 'narrative', 'Synthetic Arc', 'PRIVATE NARRATIVE BODY',
                     metadata={'arc_key': 'synthetic'})
        for number in range(105):
            key = f'scene_{number:03}'
            store.create(key, 'scene', f'Scene {number:03}', f'PRIVATE MATERIAL BODY {number:03}',
                         metadata={'date': f'2026-01-{number % 28 + 1:02}'})
            store.conn.execute("INSERT INTO narrative_materials VALUES "
                               "('narrative_menu',1,'fixture','scene',?,'linked','{}')", (key,))
        source = store.add_source('synthetic/original', 'PRIVATE EVIDENCE BODY')
        store.bind('scene_000', source)
    refs = [{'kind': 'scene', 'id': 'scene_000'}]
    feeling = Feelings(database).create('A separately authored feeling.', refs, author='Synthetic')
    plan = Plans(database).create('An explicit intention.', actor='Synthetic', related=refs)
    Personal(database).annotate('scene_000', 'A separately authored note.', 'Synthetic', 'assistant', 'scene-note')
    Personal(database).annotate('narrative_menu', 'A note on the Arc.', 'Synthetic', 'assistant', 'arc-note')
    return database, feeling['id'], plan['id']


def test_public_materials_and_explicit_picks_keep_annotations_without_reverse_links(menu_database, monkeypatch):
    database, feeling_id, plan_id = menu_database

    def unexpected(*_args, **_kwargs):
        raise AssertionError('Default materials and explicit picks must not reverse-scan feel/plan')

    monkeypatch.setattr('serein.core.feelings.backlinks', unexpected)
    monkeypatch.setattr('serein.core.plans.backlinks', unexpected)
    with Reader(database) as reader:
        public = reader.materials('narrative_menu', limit=100, with_evidence=True)
        lean = reader.materials('narrative_menu', limit=100, with_evidence=True, with_related=False)
        assert without_enrichment(public) == lean
        item = public['items'][0]['object']
        assert 'feelings' not in item and 'plans' not in item
        assert item['annotations'][0]['content'] == 'A separately authored note.'
        assert item['evidence'][0]['content'] == 'PRIVATE EVIDENCE BODY'
        assert not RELATED_FIELDS & lean['items'][0]['object'].keys()
        _, menus = _arcs(reader, [], arc_key='synthetic')
        index = next(row['index'] for row in menus['synthetic']['all_materials'] if row['id'] == 'scene_000')
        picks = read_arc_picks(reader, 'synthetic', [index, 0], with_evidence=True)['items']
        assert picks[0]['object'] == item
        assert picks[1]['object']['annotations'][0]['content'] == 'A note on the Arc.'
        assert picks[1]['object']['document']['body_md'] == 'PRIVATE NARRATIVE BODY'
        assert reader.store.conn.total_changes == 0


def test_paged_menu_and_render_match_without_backlinks_or_annotation_queries(menu_database, monkeypatch):
    database, _, _ = menu_database
    with FullAssociationsReader(database) as reader:
        hit = {'id': 'scene_000', 'kind': 'scene', 'score': .8, 'object': reader.read('scene_000')}
        expected_arcs = _arcs(reader, [hit])
        expected_render = render([hit], reader=reader)

    def unused_enrichment(*args, **kwargs):
        raise AssertionError('An Arc menu must not load unused reverse associations')

    monkeypatch.setattr('serein.core.feelings.backlinks', unused_enrichment)
    monkeypatch.setattr('serein.core.plans.backlinks', unused_enrichment)
    with Reader(database) as reader:
        statements = []
        reader.store.conn.set_trace_callback(statements.append)
        actual_arcs = _arcs(reader, [hit])
        actual_render = render([hit], reader=reader)
        reader.store.conn.set_trace_callback(None)
        assert actual_arcs == expected_arcs
        assert actual_render == expected_render
        menu = actual_arcs[1]['synthetic']
        assert menu['material_count'] == 106 and menu['menu_truncated']
        assert [row['index'] for row in menu['all_materials']] == list(range(106))
        assert [row['index'] for row in menu['materials']] == [0, 1, 2, 3, 4, 101, 102, 103, 104, 105]
        assert actual_render['menu_fingerprints'] == {'synthetic': menu['menu_fingerprint']}
        assert all(row['date'].startswith('2026-01-') for row in menu['all_materials'][1:])
        serialized = json.dumps(actual_render)
        assert 'PRIVATE NARRATIVE BODY' not in serialized
        assert 'PRIVATE MATERIAL BODY 001' not in serialized
        assert 'PRIVATE EVIDENCE BODY' not in serialized
        assert not any('personal_records' in sql.lower() or 'json_each' in sql.lower() for sql in statements)
        assert reader.store.conn.total_changes == 0


@pytest.mark.parametrize('include_mentions', [False, True])
@pytest.mark.parametrize('with_evidence', [False, True])
def test_lean_materials_preserve_aliases_exclusions_locks_history_and_pages(
        read_database, include_mentions, with_evidence):
    options = {'include_mentions': include_mentions, 'with_evidence': with_evidence,
               'at': '2026-05-04T12:00:00+08:00'}
    with Reader(read_database) as reader:
        full = reader.materials('narrative_a', limit=100, **options)
        lean = reader.materials('narrative_a', limit=100, with_related=False, **options)
        assert without_enrichment(full) == lean
        first = reader.materials('narrative_a', limit=4, with_related=False, **options)
        second = reader.materials('narrative_a', offset=first['next_offset'], limit=100,
                                  with_related=False, **options)
        assert first['items'] + second['items'] == lean['items']
        assert second['next_offset'] is None
        items = {row['id']: row for row in lean['items']}
        assert items['event_live']['selection'] == 'excluded' and items['event_live']['object'] is None
        assert len(items['event_a']['references']) == 2
        assert items['event_a']['object']['readable']
        assert items['scene_deleted']['object']['status'] == 'deleted'
        assert items['event_missing']['object']['status'] == 'missing'
        assert items['3']['object']['status'] == 'locked'
        assert set(items['1']['requested_ids']) == {'1', 'diary-vps-1'}
        assert items['scene_mention']['selection'] == ('selected' if include_mentions else 'mention_only')
        assert len(items['scene_a']['object']['evidence']) == int(with_evidence)
        assert reader.materials('narrative_a', revision=1, with_related=False, **options)['items'] == []
        assert reader.store.conn.total_changes == 0


@pytest.mark.parametrize('source_state', ['changed', 'locked', 'deleted'])
def test_exact_diary_source_alias_never_falls_through_to_numeric_body(tmp_path, diary_snapshot, source_state):
    database = tmp_path / 'exact-source.db'
    alias = 'diary_source_20260504_exact'
    sha = digest(diary_snapshot['diaries'][0]['content'])
    ledger = f'| 2026-05-04 | Assistant-Diary `#1` title | `{alias}` | `{sha}` | source |\n'
    with Store(database) as store:
        store.create('narrative_source', 'narrative', 'Synthetic source Arc', ledger,
                     metadata={'arc_key': 'source'})
        import_diaries(store, diary_snapshot)
        for requested in ('1', alias):
            store.conn.execute("INSERT INTO narrative_materials VALUES "
                               "('narrative_source',1,?,'diary',?,'linked','{}')", (requested, requested))
        if source_state == 'changed':
            store.conn.execute("UPDATE diary_entries SET body_md='NEW CURRENT PRIVATE BODY',"
                               "metadata_json=json_set(metadata_json,'$.content','NEW CURRENT PRIVATE BODY') WHERE id=1")
        elif source_state == 'locked':
            store.conn.execute("UPDATE diary_entries SET visibility='locked' WHERE id=1")
        else:
            store.conn.execute("UPDATE diary_entries SET visibility='deleted',deleted_at='synthetic deletion' WHERE id=1")
    with Reader(database) as reader:
        public = reader.materials('narrative_source')
        lean = reader.materials('narrative_source', with_related=False)
        assert public == lean
        assert len(lean['items']) == 1
        obj = lean['items'][0]['object']
        assert obj['requested_id'] == alias and not obj['readable']
        assert obj['status'] == {'changed': 'historical_source', 'locked': 'locked', 'deleted': 'deleted'}[source_state]
        assert obj['document'] is None and obj['comments'] == []
        assert 'NEW CURRENT PRIVATE BODY' not in encode(lean)
        if source_state == 'changed':
            assert reader.read('1', kind='diary')['document']['body_md'] == 'NEW CURRENT PRIVATE BODY'
        assert reader.store.conn.total_changes == 0


@pytest.mark.parametrize('source_state', ['locked', 'deleted'])
def test_lean_feel_members_keep_frozen_and_current_source_guards(tmp_path, source_state):
    database = tmp_path / 'feel-source.db'
    with Store(database) as store:
        store.create('event_source', 'event', 'Original trigger title', 'Original trigger prose')
        store.create('narrative_feel', 'narrative', 'Synthetic feelings', 'Narrative',
                     metadata={'arc_key': 'feel'})
    feel = Feelings(database).create('An independently authored feeling.',
                                    [{'kind': 'event', 'id': 'event_source'}], author='Synthetic', can_surface=True)
    with Store(database) as store:
        store.conn.execute("INSERT INTO narrative_materials VALUES "
                           "('narrative_feel',1,'fixture','scene',?,'linked','{}')", (feel['id'],))
        store.revise('event_source', expected_revision=1, title='New trigger title', body_md='New trigger prose')
    with Reader(database) as reader:
        obj = reader.materials('narrative_feel', with_related=False)['items'][0]['object']
        assert obj['feel_sources'][0]['summary'] == 'Original trigger prose'
        assert obj['feel_sources'][0]['revision'] == 1 and obj['feel_sources'][0]['current_revision'] == 2
        assert obj['feel_sources'][0]['sha256'] == digest('Original trigger prose')
    with Store(database) as store:
        if source_state == 'locked':
            store.revise('event_source', expected_revision=2, title='Private trigger title',
                         body_md='Private trigger prose', metadata={'locked': True})
        else:
            store.set_lifecycle('event_source', 'deleted')
    with Reader(database) as reader:
        public = reader.materials('narrative_feel')
        lean = reader.materials('narrative_feel', with_related=False)
        assert without_enrichment(public) == lean
        obj = lean['items'][0]['object']
        assert obj['readable'] and obj['memory_role'] == 'feel'
        assert obj['feel_sources'][0]['status'] == ('restricted' if source_state == 'locked' else 'unavailable')
        assert not obj['surface_state']['can_surface'] and obj['evidence'] == []
        assert 'Original trigger prose' not in encode(lean) and 'Private trigger' not in encode(lean)
        assert reader.store.conn.total_changes == 0


@pytest.mark.parametrize('owner_state', ['locked', 'deleted'])
def test_unreadable_narrative_owner_has_no_menu_or_materials(tmp_path, owner_state):
    database = tmp_path / 'unreadable-owner.db'
    with Store(database) as store:
        store.create('scene_public', 'scene', 'Public Scene', 'Readable body')
        store.create('narrative_private', 'narrative', 'Private Arc title', 'Private Arc body',
                     metadata={'arc_key': 'private', 'locked': owner_state == 'locked'})
        store.conn.execute("INSERT INTO narrative_materials VALUES "
                           "('narrative_private',1,'fixture','scene','scene_public','linked','{}')")
        if owner_state == 'deleted':
            store.set_lifecycle('narrative_private', 'deleted')
    with Reader(database) as reader:
        for related in (True, False):
            page = reader.materials('narrative_private', with_related=related)
            assert page['status'] == owner_state and page['items'] == [] and page['total'] == 0
        assert _arcs(reader, [{'id': 'scene_public', 'kind': 'scene'}]) == ({}, {})
        assert read_arc_picks(reader, 'private', [0])['status'] == 'not_found'
        assert reader.store.conn.total_changes == 0
