import sqlite3

import pytest

from test_live_clients import live
from test_live_narratives import seed
from serein.compat.narratives import narrative_transaction, RevisionInbox, Narratives


@pytest.mark.parametrize('collecting', [False, True])
def test_delete_preserves_sources_and_history_and_removes_arc_and_hints(live, collecting):
    settings, client = live
    event, scene = seed(client, settings)
    with narrative_transaction(settings.database, write=True) as rolls:
        if collecting:
            current = rolls.read('narrative_test')
            rolls.publish(narrative_id='narrative_test', expected_revision=current['revision'],
                title='雨声', document=f'# 雨声\n\n## 第一人称叙事\n\n## 来源账\n{event}\n{scene}',
                arc_key='arc:rain', publication_status='collecting',
                source_event_ids=[event], source_scene_ids=[scene])
        before = rolls.store.read('narrative_test')
        source_before = {key: rolls.store.read(key) for key in (event, scene)}
        rolls.store.conn.execute('INSERT INTO fact_event_arc_links VALUES (?,?,?,?)', ('arc:rain', event, 'hash', '2026-01-01'))
        rolls.store.conn.execute('INSERT INTO event_arc_links VALUES (?,?,?,?)', ('arc:rain', event, 'hash', '{}'))
        RevisionInbox(rolls.store)._save({'items': [
            {'proposal_id': 'hint', 'narrative_id': 'narrative_test', 'status': 'pending'},
            {'proposal_id': 'other', 'narrative_id': 'narrative_other', 'status': 'pending'}]})
    body = {'narrative_id': 'narrative_test', 'expected_revision': before['revision'], 'confirm': 'DELETE'}
    assert client.post('/api/narrative-rolls/delete', json={**body, 'confirm': ''}).status_code == 400
    stale = client.post('/api/narrative-rolls/delete', json={**body, 'expected_revision': 999})
    assert stale.status_code == 409 and stale.json()['reason'] == 'narrative_revision_changed'
    assert client.post('/api/narrative-rolls/delete', json=body).json()['status'] == 'deleted'
    assert client.post('/api/narrative-rolls/delete', json=body).json()['status'] == 'deleted'
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').status_code == 404
    assert client.get('/api/narrative-rolls').json()['items'] == []
    with narrative_transaction(settings.database) as rolls:
        deleted = rolls.store.read('narrative_test')
        assert deleted['lifecycle'] == 'deleted'
        assert deleted['body_md'] == before['body_md'] and deleted['revision'] == before['revision']
        assert rolls.store.conn.execute('SELECT count(*) FROM deletions WHERE document_id=?', ('narrative_test',)).fetchone()[0] == 1
        assert rolls.store.conn.execute('SELECT count(*) FROM index_outbox WHERE document_id=?', ('narrative_test',)).fetchone()[0] > 0
        for table in ('event_arc_links', 'fact_event_arc_links'):
            assert rolls.store.conn.execute(f'SELECT count(*) FROM {table} WHERE arc_key=?', ('arc:rain',)).fetchone()[0] == 0
        for key, original in source_before.items():
            assert rolls.store.read(key) == original
        hints = RevisionInbox(rolls.store)._load()['items']
        assert hints[0]['resolution'] == 'narrative_deleted' and hints[0]['status'] == 'dismissed'
        assert hints[1]['status'] == 'pending'


@pytest.mark.parametrize('code,reason', [
    (sqlite3.SQLITE_BUSY, 'storage_busy'), (sqlite3.SQLITE_READONLY, 'storage_read_only'),
    (sqlite3.SQLITE_FULL, 'storage_full'), (sqlite3.SQLITE_CORRUPT, 'storage_corrupt')])
def test_storage_failures_are_distinct_and_rollback(live, monkeypatch, code, reason):
    settings, client = live
    seed(client, settings)
    def fail(self, *args):
        self.store.set_lifecycle('narrative_test', 'deleted')
        error = sqlite3.OperationalError('synthetic failure')
        error.sqlite_errorcode = code
        raise error
    monkeypatch.setattr(Narratives, 'delete', fail)
    response = client.post('/api/narrative-rolls/delete', json={
        'narrative_id': 'narrative_test', 'expected_revision': 1, 'confirm': 'DELETE'})
    assert response.status_code == 503 and response.json()['reason'] == reason
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').status_code == 200
