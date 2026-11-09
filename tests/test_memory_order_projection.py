"""Memory-list clocks retain source meaning and never promote an import date."""
import pytest

from serein.api.scenes import memory_time_projection
from serein.core.feelings import Feelings
from serein.core.store import Store
from test_live_clients import live


@pytest.mark.parametrize('metadata,expected', [
    ({'date': '2026-08-01', 'created': '2026-11-09T01:00:01Z'},
     ('2026-08-01', '2026-08-01', 'date', 'day', '2026-11-09T01:00:01Z')),
    ({'event_date': '2026-08-02', 'created': '2026-11-09T01:00:01Z'},
     ('2026-08-02', '2026-08-02', 'event_date', 'day', '2026-11-09T01:00:01Z')),
    ({'date': '2026-11-08T23:30:00-05:00'},
     ('2026-11-09', '2026-11-08T23:30:00-05:00', 'date', 'timestamp', '2099-01-01T00:00:00Z')),
    ({'created': '2026-11-08T20:01:02Z'},
     ('2026-11-09', '2026-11-08T20:01:02Z', 'created', 'timestamp', '2026-11-08T20:01:02Z')),
    ({'created': '2026-11-08T20:01:02'},
     ('2026-11-09', '2026-11-08T20:01:02', 'created:assumed_utc', 'timestamp', '2026-11-08T20:01:02')),
    ({'memory_role': 'feel', 'authored_at': '2026-11-09T01:02:03+08:00'},
     ('2026-11-09', '2026-11-09T01:02:03+08:00', 'authored_at', 'timestamp', '2026-11-09T01:02:03+08:00')),
    ({'memory_role': 'feel', 'authored_at': None, 'imported_at': '2026-11-09T01:00:00Z'},
     ('', '', 'unknown', 'unknown', '')),
    ({'import_format': 'ob-backup-v1', 'authored_time': {
        'value': '2026-08-03T18:01:02+08:00', 'basis': 'original_explicit_offset'}},
     ('2026-08-03', '2026-08-03T18:01:02+08:00', 'authored_time:original_explicit_offset', 'timestamp', '2026-08-03T18:01:02+08:00')),
    ({'import_format': 'ob-backup-v1', 'authored_time': {'value': None, 'basis': 'unknown'}},
     ('', '', 'unknown', 'unknown', '')),
    ({'import_format': 'ombre-legacy', 'date': '', 'created': ''},
     ('', '', 'unknown', 'unknown', '')),
    ({'imported_at': '2026-11-09T01:00:00Z', 'authored_at': None,
      'memory_value_source': 'legacy_authored_self'},
     ('', '', 'unknown', 'unknown', '')),
    ({'date': '2026-02-31', 'created': '2026-08-03T18:01:02+08:00'},
     ('2026-08-03', '2026-08-03T18:01:02+08:00', 'created', 'timestamp', '2026-08-03T18:01:02+08:00')),
    ({}, ('2099-01-01', '2099-01-01T00:00:00Z', 'stored_at', 'timestamp', '2099-01-01T00:00:00Z')),
])
def test_projection_uses_original_clock_and_preserves_declared_days(metadata, expected):
    doc = {'metadata': metadata, 'created_at': '2099-01-01T00:00:00Z'}
    result = memory_time_projection(doc)
    assert tuple(result[key] for key in ('date', 'memory_time', 'memory_time_basis',
                                         'memory_time_precision', 'recorded_at')) == expected
    assert doc['metadata'] == metadata


def test_live_projection_includes_feel_time_and_keeps_reader_permissions(live):
    settings, client = live
    with Store(settings.database) as store:
        store.create('clock-source', 'event', 'Source', 'A source event.')
        store.create('clock-explicit', 'scene', 'Explicit older day', 'A dated scene.',
                     metadata={'date': '2026-08-01', 'created': '2026-11-09T01:00:00Z'})
        store.create('clock-locked', 'scene', 'Locked', 'A locked scene.',
                     metadata={'locked': True, 'created': '2099-01-01T00:00:00Z'})
        store.create('clock-deleted', 'scene', 'Deleted', 'A deleted scene.', lifecycle='deleted')
    feel = Feelings(settings.database).create('A feeling with a source.',
        [{'kind': 'event', 'id': 'clock-source'}], author='Fixture',
        authored_at='2026-11-08T23:30:00-05:00')
    response = client.post('/api/serein/memory-projection', json={})
    assert response.status_code == 200
    scenes = {item['source_id']: item for item in response.json()['scenes']}
    assert scenes[feel['id']]['date'] == '2026-11-09'
    assert scenes[feel['id']]['memory_time'] == scenes[feel['id']]['recorded_at'] == '2026-11-08T23:30:00-05:00'
    assert scenes[feel['id']]['memory_time_basis'] == 'authored_at'
    assert scenes['clock-explicit']['date'] == scenes['clock-explicit']['memory_time'] == '2026-08-01'
    assert scenes['clock-explicit']['recorded_at'] == '2026-11-09T01:00:00Z'
    assert 'clock-locked' not in scenes and 'clock-deleted' not in scenes
    client.headers.pop('Authorization')
    assert client.post('/api/serein/memory-projection', json={}).status_code == 401
