import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from serein.core.store import Store
from serein import host_persona
from test_live_clients import live


def payload(role, key, *, origin='live', completed=True, session='orangechat:a:chat'):
    meta = {'origin': origin}
    if role == 'assistant':
        meta['generation_completed'] = completed
    return {'source': 'orangechat', 'client': 'orangechat', 'session_id': session,
            'conversation_id': 'chat', 'events': [{
                'source_event_id': key, 'role': role, 'text': 'Synthetic ' + role,
                'created_at': datetime.now(timezone.utc).isoformat(), 'metadata': meta}]}


@pytest.fixture
def evaluations(monkeypatch):
    calls = []
    monkeypatch.setattr(host_persona, 'read_settings', lambda database: {'features': {'persona': True, 'anti_retreat': False}})
    monkeypatch.setattr(host_persona, 'task_model', lambda *args: {'model': 'synthetic'})
    async def evaluate(*args, **kwargs):
        calls.append((args, kwargs))
        return True
    monkeypatch.setattr('serein.chat_features.after_reply', evaluate)
    return calls


def test_new_live_completed_upload_updates_once_after_archive(live, evaluations):
    settings, client = live
    user = payload('user', 'user:1')
    assistant = payload('assistant', 'assistant:1')
    client.post('/api/ingest-raw', json=user).raise_for_status()
    assert evaluations == []
    result = client.post('/api/ingest-raw', json=assistant)
    result.raise_for_status()
    assert result.json()['inserted'] == 1
    assert len(evaluations) == 1
    args, options = evaluations[0]
    assert args[1:4] == ('orangechat:a:chat', 'Synthetic user', 'Synthetic assistant')
    assert options['force_persona'] is True and options['round_id'] == 1
    retry = client.post('/api/ingest-raw', json=assistant)
    assert retry.json()['duplicate'] == 1 and len(evaluations) == 1
    with Store(settings.database, read_only=True) as store:
        rows = store.conn.execute("SELECT value_json FROM background_state WHERE name LIKE 'host_persona:%'").fetchall()
        assert len(rows) == 1 and json.loads(rows[0][0])['status'] == 'evaluated'
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['history', 'incomplete', 'other_session', 'old', 'naive', 'other_client'])
def test_imports_partial_old_and_unpaired_messages_never_change_mood(live, evaluations, change):
    settings, client = live
    client.post('/api/ingest-raw', json=payload('user', 'user:1')).raise_for_status()
    body = payload('assistant', 'assistant:1')
    if change == 'history': body['events'][0]['metadata']['origin'] = 'history'
    if change == 'incomplete': body['events'][0]['metadata']['generation_completed'] = False
    if change == 'other_session': body['session_id'] = 'other'
    if change == 'old': body['events'][0]['created_at'] = '2020-01-01T00:00:00Z'
    if change == 'naive': body['events'][0]['created_at'] = '2030-01-01T00:00:00'
    if change == 'other_client': body['client'] = 'archive-import'
    client.post('/api/ingest-raw', json=body).raise_for_status()
    assert evaluations == []


def test_mood_failure_does_not_lose_archive_or_retry_model(live, evaluations, monkeypatch):
    settings, client = live
    async def fail(*args, **kwargs):
        evaluations.append(True)
        raise RuntimeError('Synthetic upstream failure')
    monkeypatch.setattr('serein.chat_features.after_reply', fail)
    client.post('/api/ingest-raw', json=payload('user', 'user:1')).raise_for_status()
    body = payload('assistant', 'assistant:1')
    result = client.post('/api/ingest-raw', json=body)
    result.raise_for_status()
    assert result.json()['inserted'] == 1
    client.post('/api/ingest-raw', json=body).raise_for_status()
    assert len(evaluations) == 1
    with Store(settings.database, read_only=True) as store:
        row = store.conn.execute("SELECT value_json FROM background_state WHERE name LIKE 'host_persona:%'").fetchone()
        assert json.loads(row[0])['status'] == 'failed'


def test_disabled_mood_does_not_claim_or_call(live, evaluations, monkeypatch):
    settings, client = live
    monkeypatch.setattr(host_persona, 'read_settings', lambda db: {'features': {'persona': False, 'anti_retreat': False}})
    client.post('/api/ingest-raw', json=payload('user', 'user:1')).raise_for_status()
    client.post('/api/ingest-raw', json=payload('assistant', 'assistant:1')).raise_for_status()
    assert evaluations == []


@pytest.mark.parametrize('valid', [True, False], ids=['mood-updated', 'malformed-json-recorded'])
def test_uploaded_exchange_runs_real_persona_engine_and_records_actual_outcome(live, monkeypatch, valid):
    settings, client = live
    client.patch('/v1/settings', json={
        'models': [{'id': 'persona', 'label': 'Synthetic persona', 'model': 'persona-model',
                    'base_url': 'http://127.0.0.1:9/v1'}],
        'assignments': {'persona': 'persona'},
        'features': {'persona': True, 'anti_retreat': False},
    }).raise_for_status()
    calls = []

    def handle(request):
        body = json.loads(request.content)
        assert body['model'] == 'persona-model'
        exchange = json.loads(body['messages'][1]['content'])
        assert exchange['latest_user_message'] == 'Synthetic user'
        assert exchange['assistant_response'] == 'Synthetic assistant'
        calls.append(body)
        evaluation = {'event_type': 'affection', 'inner_thought': 'Synthetic changed mood',
                      'surface_trigger': 'Synthetic live exchange', 'mood_label': 'warm',
                      'affect_delta': {'tenderness': .15, 'security': .12},
                      'relationship_event': True, 'relationship_delta': {'trust': .02},
                      'confidence': .9}
        content = json.dumps(evaluation) if valid else 'Synthetic malformed evaluation'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    client.post('/api/ingest-raw', json=payload('user', 'user:1')).raise_for_status()
    body = payload('assistant', 'assistant:1')
    response = client.post('/api/ingest-raw', json=body)
    response.raise_for_status()
    assert response.json()['inserted'] == 1 and len(calls) == 1
    retry = client.post('/api/ingest-raw', json=body)
    assert retry.json()['duplicate'] == 1 and len(calls) == 1
    with Store(settings.database, read_only=True) as store:
        saved = json.loads(store.conn.execute(
            "SELECT value_json FROM background_state WHERE name LIKE 'host_persona:%'").fetchone()[0])
        assert saved['status'] == 'evaluated'
        session = store.conn.execute('SELECT * FROM persona_session_state WHERE session_id=?',
                                     ('orangechat:a:chat',)).fetchone()
        events = store.conn.execute('SELECT error FROM persona_events WHERE session_id=?',
                                    ('orangechat:a:chat',)).fetchall()
        assert session is not None and len(events) == 1
        if valid:
            assert session['tenderness'] == pytest.approx(.15)
            assert session['security'] == pytest.approx(.62)
            assert session['inner_thought'] == 'Synthetic changed mood' and not events[0]['error']
        else:
            assert session['tenderness'] == 0 and session['security'] == .5
            assert session['inner_thought'] == '' and events[0]['error'] == 'persona LLM returned malformed JSON'
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
