"""Background lexical preparation preserves eligibility without Event surfacing work."""
from dataclasses import replace

import pytest

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.feelings import Feelings
from serein.core.reader import Reader
from serein.core.store import Store, promoted_scene_id
from serein.recall import legacy_indexes
from serein.recall.index import Search, build_index, content_stamp, refresh_index


def prepared_events(tmp_path):
    settings = Settings(tmp_path / 'synthetic.db', tmp_path / 'index.db', writable=True)
    initialize(replace(settings, index=None))
    with Store(settings.database) as store:
        for name, manual in [('normal', True), ('disabled', False), ('unreviewed', None),
                             ('covered', True), ('replaced', True), ('promoted', True)]:
            store.create('event_' + name, 'event', name, 'Synthetic event body',
                         manual_surface=manual, metadata={'custom': {'keep': name},
                                                        'item_id': 'legacy', 'status': 'legacy'})
        store.create('event_successor', 'event', 'Successor', 'Synthetic successor')
        store.conn.execute("INSERT INTO event_replacements VALUES ('event_replaced','event_successor','fixture','{}')")
        store.create(promoted_scene_id('event_promoted'), 'scene', 'Recognition', 'Synthetic recognition',
                     metadata={'promoted_from_event': {'id': 'event_promoted'}})
        source = store.add_source('synthetic:covered', 'Synthetic exact evidence')
        store.bind('event_covered', source)
        store.create('scene_cover', 'scene', 'Recognition', 'Synthetic recognition')
        store.bind('scene_cover', source)
        for name, metadata in [('locked', {'locked': True}), ('invisible', {'visibility': 'deleted'}),
                               ('future', {'unlock_at': '2999-01-01T00:00:00Z'}),
                               ('invalid_time', {'unlock_at': 'invalid'})]:
            store.create('event_' + name, 'event', name, 'Synthetic restricted body', metadata=metadata)
        for state in ('archived', 'deleted', 'superseded'):
            store.create('event_' + state, 'event', state, 'Synthetic inactive body', lifecycle=state)
        store.create('event_tombstone', 'event', 'Tombstone', 'Synthetic unavailable body')
        store.record_deletion('event_tombstone', '2026-01-01T00:00:00Z', {})
        store.conn.execute("UPDATE documents SET lifecycle='active' WHERE id='event_tombstone'")
        store.create('event_stale', 'event', 'Stale', 'Original synthetic body')
        store.create('scene_disabled', 'scene', 'Quiet recognition', 'Synthetic recognition', manual_surface=False)
    build_index(settings.database, settings.index)
    with Store(settings.database) as store:
        store.revise('event_stale', expected_revision=1, title='Changed', body_md='Changed synthetic body')
    return settings


def previous_event_payloads(settings):
    expected = []
    with Search(settings.database, settings.index) as search:
        for row in search.conn.execute("SELECT * FROM documents WHERE kind IN ('scene','event')"):
            obj = search.reader.read(row['id'], with_evidence=False, with_related=False)
            if not obj['readable'] or obj['document']['lifecycle'] != 'active':
                continue
            doc = obj['document']
            if doc['kind'] == 'event' and content_stamp(doc) == row['stamp']:
                expected.append({**doc['metadata'], 'item_id': doc['id'], 'item_type': 'event',
                                 'title': doc['title'], 'body': doc['body_md'], 'status': 'active'})
    return expected


def test_event_payloads_keep_hidden_coverage_replacement_and_access_contract(tmp_path, monkeypatch):
    settings = prepared_events(tmp_path)
    expected = previous_event_payloads(settings)
    captured = []

    class Lexical:
        def sync(self, events, **kwargs):
            captured.extend(events)
            return {'status': 'synthetic'}

    monkeypatch.setattr(legacy_indexes, 'lexical_index', lambda _settings: Lexical())
    original_read = Reader.read
    original_surface = Store.surface_state
    scene_reads = []

    def no_event_read(self, identifier, **kwargs):
        assert not str(identifier).startswith('event_'), 'Event eligibility must not materialize surface state'
        scene_reads.append(identifier)
        return original_read(self, identifier, **kwargs)

    def no_event_surface(self, identifier):
        assert not str(identifier).startswith('event_'), 'Lexical Events do not consume surfacing decisions'
        return original_surface(self, identifier)

    monkeypatch.setattr(Reader, 'read', no_event_read)
    monkeypatch.setattr(Store, 'surface_state', no_event_surface)
    _enabled, scenes, _passages, _report = legacy_indexes._prepare_refresh(settings, dry_run=True)
    assert captured == expected
    assert {row['item_id'] for row in captured} == {
        'event_' + name for name in ('normal', 'disabled', 'unreviewed', 'covered', 'replaced', 'promoted', 'successor')}
    assert all(row['status'] == 'active' and row['item_type'] == 'event' for row in captured)
    assert scene_reads and 'scene_cover' in scene_reads
    assert 'scene_disabled' not in {scene['id'] for scene in scenes}


def test_scene_second_read_stamp_is_authoritative(tmp_path, monkeypatch):
    settings = prepared_events(tmp_path)
    original_read = Reader.read
    changed = []

    def change_during_read(self, identifier, **kwargs):
        if identifier == 'scene_cover' and not changed:
            with Store(settings.database) as store:
                store.revise(identifier, expected_revision=1, title='Changed scene', body_md='Changed synthetic recognition')
            changed.append(True)
        return original_read(self, identifier, **kwargs)

    monkeypatch.setattr(Reader, 'read', change_during_read)
    _enabled, scenes, _passages, _report = legacy_indexes._prepare_refresh(settings, dry_run=True)
    assert changed
    assert 'scene_cover' not in {scene['id'] for scene in scenes}


@pytest.mark.parametrize('source_state,expected', [
    ('active', True), ('archived', True), ('locked', False), ('deleted', False),
])
def test_feel_eligibility_still_checks_current_access_to_frozen_event_source(
        tmp_path, monkeypatch, source_state, expected):
    settings = prepared_events(tmp_path)
    feel = Feelings(settings.database).create(
        'I felt calm about this synthetic event.', [{'kind': 'event', 'id': 'event_normal'}],
        author='Synthetic', can_surface=True)
    refresh_index(settings.database, settings.index, [feel['id']])
    # The Feel keeps the original source revision. Its current source access
    # still governs surfacing after a later lock, archive or deletion.
    with Store(settings.database) as store:
        if source_state == 'locked':
            source = store.read('event_normal')
            store.revise('event_normal', expected_revision=source['revision'], title=source['title'],
                         body_md=source['body_md'], metadata={'locked': True})
        elif source_state != 'active':
            store.set_lifecycle('event_normal', source_state)

    class Lexical:
        def sync(self, events, **kwargs):
            return {'status': 'synthetic'}

    monkeypatch.setattr(legacy_indexes, 'lexical_index', lambda _settings: Lexical())
    _enabled, scenes, _passages, _report = legacy_indexes._prepare_refresh(settings, dry_run=True)
    assert (feel['id'] in {scene['id'] for scene in scenes}) is expected
