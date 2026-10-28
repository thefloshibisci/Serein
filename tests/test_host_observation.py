import copy
import pytest
from fastapi.testclient import TestClient
from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(tmp_path/'serein.db', tmp_path/'index.sqlite', writable=True)
    initialize(settings)
    with TestClient(create_app(settings, token='test', live=True), headers={'Authorization':'Bearer test'}) as client:
        yield client


def receipt():
    return {'receipt_id':'orangechat:synthetic', 'window_id':'orangechat:a:chat',
            'delivered_ids':['scene:test'], 'observation':{
                'version':1, 'created_at':'2026-10-28T01:27:39Z', 'elapsed_ms':123,
                'request_status':'completed', 'recall_status':'ready', 'query':'Synthetic query',
                'route':'memory', 'reason':'relevant',
                'candidates':[{'id':'scene:test', 'title':'Synthetic title', 'source_kind':'scene', 'score':.91}]}}


def test_receipt_is_idempotent_projected_with_details_and_supports_refresh(client):
    payload = receipt()
    for _ in range(2):
        response = client.post('/v1/host/deliveries', json=payload)
        assert response.status_code == 200
        assert response.json()['observation_version'] == 1
        assert response.json()['receipt_id'] == payload['receipt_id']
    rows = client.get('/v1/host/deliveries').json()['items']
    assert len(rows) == 1
    row = rows[0]
    assert row['hook_memory_outcome'] == 'injected'
    assert row['query'] == 'Synthetic query' and row['created_at'].endswith('Z')
    assert row['gateway_memory_items'][0]['title'] == 'Synthetic title'
    assert row['elapsed_ms'] == 123 and row['reported_by'] == 'orangechat'
    assert client.get(f"/v1/host/deliveries?after_id={row['id']}").json()['items'] == []
    assert client.get(f"/v1/host/deliveries?review_ids={row['id']}").status_code == 200
    payload['observation']['query'] = 'different'
    assert client.post('/v1/host/deliveries', json=payload).status_code == 400


@pytest.mark.parametrize('status,recall,outcome', [('failed','ready','failed'),('interrupted','ready','failed'),
    ('completed','empty','no_match'),('completed','timeout','failed'),('completed','ready','skip')])
def test_candidates_are_never_promoted_to_delivered(client, status, recall, outcome):
    payload = receipt(); payload['delivered_ids'] = []
    payload['observation'].update(request_status=status, recall_status=recall)
    assert client.post('/v1/host/deliveries', json=payload).status_code == 200
    row = client.get('/v1/host/deliveries').json()['items'][0]
    assert row['hook_memory_outcome'] == outcome
    assert row['gateway_memory_items'] == [] and len(row['prepared_items']) == 1


def test_old_service_receipt_can_be_enriched_but_not_rewritten(client):
    full = receipt()
    legacy = {k:v for k,v in full.items() if k != 'observation'}
    assert client.post('/v1/host/deliveries', json=legacy).status_code == 200
    assert client.post('/v1/host/deliveries', json=full).status_code == 200
    assert len(client.get('/v1/host/deliveries').json()['items']) == 1
    changed = copy.deepcopy(full); changed['delivered_ids'] = []
    assert client.post('/v1/host/deliveries', json=changed).status_code == 400


@pytest.mark.parametrize('mutate', [
    lambda x:x['observation'].update(request_status='interrupted'),
    lambda x:x['observation'].update(query='x'*2001),
    lambda x:x['observation'].update(created_at='2026-10-28'),
    lambda x:x.update(delivered_ids=['scene:not-prepared']),
    lambda x:x['observation'].update(candidates=[{}]),
])
def test_invalid_metadata_rejected_without_record(client, mutate):
    payload = receipt(); mutate(payload)
    assert client.post('/v1/host/deliveries', json=payload).status_code == 400
    assert client.get('/v1/host/deliveries').json()['items'] == []


def test_unapproved_fields_are_not_stored(client):
    payload=receipt(); payload['observation'].update(gateway_key='secret', reply_body='secret', reasoning='secret')
    payload['observation']['candidates'][0]['body']='secret'
    assert client.post('/v1/host/deliveries', json=payload).status_code == 200
    assert 'secret' not in client.get('/v1/host/deliveries').text


@pytest.mark.parametrize('journal_mode', ['delete', 'wal'])
def test_receipt_waits_for_an_existing_writer(tmp_path, journal_mode):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from fastapi import FastAPI
    from serein.api.host import routes
    from serein.core.store import Store, encode

    settings = Settings(tmp_path/'serein.db', tmp_path/'index.sqlite', writable=True)
    initialize(settings)
    app = FastAPI()
    app.include_router(routes(settings, []))
    with Store(settings.database) as store:
        store.conn.execute('PRAGMA journal_mode='+journal_mode)
    locked, release = Event(), Event()
    other = {'receipt_id':'other', 'window_id':'other', 'delivered_ids':[]}

    def background_write():
        with Store(settings.database) as store, store.transaction(immediate=True):
            store.conn.execute('INSERT INTO host_deliveries(receipt_id,payload) VALUES (?,?)',
                               (other['receipt_id'], encode(other)))
            locked.set()
            release.wait(.5)

    with ThreadPoolExecutor(max_workers=1) as pool, TestClient(app) as client:
        writer = pool.submit(background_write)
        assert locked.wait(5)
        try:
            response = client.post('/v1/host/deliveries', json=receipt())
        finally:
            release.set()
            writer.result(timeout=5)
        assert response.status_code == 200
        # Both acknowledgements survive; retrying the host receipt is idempotent.
        assert client.post('/v1/host/deliveries', json=receipt()).status_code == 200
        assert {r['receipt_id'] for r in client.get('/v1/host/deliveries').json()['items']} == {
            'other', receipt()['receipt_id']}
