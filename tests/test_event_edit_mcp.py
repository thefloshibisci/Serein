"""Synthetic Event corrections through the actual MCP surface, without network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import json

import pytest

from serein.api.mcp import create_server
from serein.application import Application
from serein.compat.diaries import Diaries
from serein.compat.events import Events
from serein.compat.scenes import initialize_scene_ids
from serein.config import Settings
from serein.core.store import Store
from serein.recall.index import build_index


MODES = [False, True] if importlib.util.find_spec('serein.api.authored_tools') else [True]
BODY_LIMIT = 1500 if len(MODES) == 2 else 100000


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(tmp_path/'synthetic.db', tmp_path/'synthetic-index.db', writable=True)
    with Store(settings.database) as store:
        store.create('scene_synthetic', 'scene', 'Synthetic scene', 'Unchanged Scene')
    events = Events(settings.database, initialize=True)
    Diaries(settings.database, initialize=True)
    initialize_scene_ids(settings.database)
    build_index(settings.database, settings.index)
    result = events.write_many([{'type': 'event', 'title': 'Appointment', 'body': 'We met Tuesday.',
        'recallable': False, 'source_refs': [{'source_system': 'synthetic', 'session_id': 'one',
            'message_id': '1', 'role': 'user', 'created_at': '2026-01-01T00:00:00Z',
            'content': 'We met Wednesday.', 'binding_method': 'explicit'}]}])
    item = events.read(result['items'][0]['item_id'])
    return settings, events, item


def call(server, name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[1] if result[1] is not None else result[0]
    if isinstance(result, dict):
        return result
    text = result[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@pytest.mark.parametrize('private', MODES)
def test_mcp_correction_retains_versions_sources_and_manual_surface(runtime, private):
    settings, events, old = runtime
    server = create_server(Application(settings), private=private)
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == 'edit_event')
    assert set(tool.inputSchema['required']) == {'event_id', 'expected_updated_at'}
    assert set(tool.inputSchema['properties']) == {'event_id', 'expected_updated_at', 'title', 'content'}
    assert tool.annotations.readOnlyHint is False
    before = call(server, 'read_memory', identifier=old['item_id'], with_evidence=True)
    assert f"updated_at: {old['updated_at']}" in before
    updated = call(server, 'edit_event', event_id='event:'+old['item_id'],
                   expected_updated_at=old['updated_at'], content='We met Wednesday.')
    assert updated['status'] == 'superseded' and updated['event_id'] != old['item_id']
    assert set(updated) == {'status', 'previous_event_id', 'event_id', 'updated_at'}
    previous = events.read(old['item_id'])
    assert previous['body'] == old['body'] and previous['status'] == 'superseded'
    current = events.read(updated['event_id'])
    assert current['title'] == old['title'] and current['body'] == 'We met Wednesday.'
    assert current['recallable'] is False
    fields = ('source_system', 'session_id', 'message_id', 'role', 'content', 'content_sha256')
    assert [{k: r[k] for k in fields} for r in current['source_refs']] == [
        {k: r[k] for k in fields} for r in old['source_refs']]
    read = call(server, 'read_memory', identifier=updated['event_id'], with_evidence=True)
    assert 'We met Wednesday.' in read and 'We met Tuesday.' not in read
    stale = call(server, 'edit_event', event_id=old['item_id'], expected_updated_at=old['updated_at'],
                 content='Stale overwrite')
    assert stale['status'] == 'conflict'
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 2
        assert store.read(old['item_id'])['body_md'] == old['body']
        assert store.read(updated['event_id'])['body_md'] == current['body']


@pytest.mark.parametrize('private', MODES)
def test_mcp_title_only_stale_timestamp_and_unchanged(runtime, private):
    settings, events, old = runtime
    server = create_server(Application(settings), private=private)
    assert call(server, 'edit_event', event_id=old['item_id'], expected_updated_at='stale',
                title='Corrected title')['status'] == 'conflict'
    assert call(server, 'edit_event', event_id=old['item_id'], expected_updated_at=old['updated_at'],
                content=old['body'])['status'] == 'unchanged'
    result = call(server, 'edit_event', event_id=old['item_id'], expected_updated_at=old['updated_at'],
                  title='Corrected title')
    assert events.read(result['event_id'])['body'] == old['body']
    assert events.read(result['event_id'])['title'] == 'Corrected title'


@pytest.mark.parametrize('patch', [{}, {'content': ''}, {'title': ' '}, {'content': 'x'*(BODY_LIMIT+1)},
                                  {'content': 'Corrected', 'source_refs': []}])
def test_mcp_invalid_edit_never_writes(runtime, patch):
    settings, events, old = runtime
    server = create_server(Application(settings), private=True)
    with pytest.raises(Exception):
        call(server, 'edit_event', event_id=old['item_id'], expected_updated_at=old['updated_at'], **patch)
    assert events.read(old['item_id']) == old
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 1


@pytest.mark.parametrize('state', ['missing', 'scene', 'fact', 'archived', 'deleted', 'immutable'])
def test_wrong_kind_or_unavailable_event_never_writes(runtime, state):
    settings, events, old = runtime
    key = old['item_id']
    if state in ('missing', 'scene'):
        key = 'missing' if state == 'missing' else 'scene_synthetic'
    elif state == 'fact':
        key = events.write_many([{'type': 'fact', 'body': 'Synthetic fact',
                                  'source_refs': old['source_refs']}])['items'][0]['item_id']
    elif state == 'archived':
        events.set_status(key, 'archived')
    elif state == 'deleted':
        events.delete(key)
    else:
        with Store(settings.database) as store:
            doc = store.read(key)
            metadata = {**doc['metadata'], 'source_record_immutable': True}
            store.revise(key, expected_revision=doc['revision'], title=doc['title'],
                         body_md=doc['body_md'], metadata=metadata)
    with Store(settings.database, read_only=True) as store:
        count = store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]
    token = (events.read(key) or old)['updated_at']
    if state == 'immutable':
        with pytest.raises(ValueError, match='Immutable'):
            events.edit(key, token, content='Should not write')
    else:
        assert events.edit(key, token, content='Should not write')['status'] in {'not_found', 'invalid'}
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0] == count


def test_concurrent_corrections_cannot_fork_one_version(runtime):
    settings, events, old = runtime
    def edit(number):
        return Events(settings.database).edit(old['item_id'], old['updated_at'], content=f'Correction {number}')
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(edit, [1, 2]))
    assert sorted(row['status'] for row in results) == ['conflict', 'superseded']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 2


def test_projection_failure_rolls_back_the_correction(runtime, monkeypatch):
    settings, events, old = runtime
    def fail(conn):
        raise ValueError('synthetic projection failure')
    monkeypatch.setattr('serein.compat.events.project_events', fail)
    with pytest.raises(ValueError, match='synthetic projection'):
        events.edit(old['item_id'], old['updated_at'], content='Correction')
    assert events.read(old['item_id']) == old
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 1


@pytest.mark.skipif(len(MODES) == 1, reason='public authored surface only')
def test_read_only_surface_does_not_expose_event_edit(runtime):
    settings, _, _ = runtime
    server = create_server(Application(replace(settings, writable=False)))
    assert 'edit_event' not in {t.name for t in asyncio.run(server.list_tools())}
