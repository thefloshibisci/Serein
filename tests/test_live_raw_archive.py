import sqlite3
from contextlib import closing

from serein.compat.germany.raw_events import RawEventStore
from serein.compat.raw_archive import seed_raw_archive
from serein.core.store import Store
from test_live_clients import live


def test_ingest_retries_preserve_raw_and_never_create_memories(live):
    settings, client = live
    payload = {'source': 'assistant_bridge_codex', 'events': [
        {'source_event_id': 'assistant-message:7', 'role': 'user', 'text': '窗边风铃响了',
         'conversation_id': 'assistant-session:1', 'session_id': 'chosen-thread', 'metadata': {'assistant_message_id': 7}},
        {'source_event_id': 'tool:8', 'role': 'tool', 'text': 'must not become dialogue'},
    ]}
    first = client.post('/api/ingest-raw', json=payload).json()
    assert (first['inserted'], first['rejected']) == (1, 1)
    retry = client.post('/api/ingest-raw', json=payload).json()
    assert retry['duplicate'] == 1
    assert retry['items'][0]['id'] == first['items'][0]['id']
    found = client.get('/api/search-raw', params={'q': '风铃', 'session_id': 'chosen-thread'}).json()
    assert found['items'][0]['metadata']['assistant_message_id'] == 7
    assert client.get('/api/search-raw', params={'q': '风铃', 'session_id': 'other-thread'}).json()['count'] == 0
    assert client.post('/api/search-raw', json={'q': '风铃'}).json()['count'] == 1
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('select count(*) from documents').fetchone()[0] == 0
    client.headers.pop('Authorization')
    assert client.post('/api/ingest-raw', json=payload).status_code == 401


def test_transplant_preserves_ids_source_rows_and_search(tmp_path):
    source = tmp_path/'raw.sqlite'; target = tmp_path/'serein.db'
    archive = RawEventStore({'raw_events': {'db_path': str(source)}})
    record = {'role': 'assistant', 'source_event_id': 'original-42', 'text': '保留旧日的风铃',
              'created_at': '2026-09-01T12:00:00Z', 'metadata': {'marker': 'unchanged'}}
    archive.ingest([record], source='original')
    archive.ingest([record], source='original')
    with Store(target):pass
    assert seed_raw_archive(target, source)['original_rows_exact']
    migrated = RawEventStore({'raw_events': {'db_path': str(target)}})
    assert migrated.search('风铃')['items'] == archive.search('风铃')['items']
    assert migrated.ingest([record], source='original')['duplicate'] == 1
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 10
