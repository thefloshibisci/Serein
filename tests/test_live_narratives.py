import re

import pytest

from test_live_clients import live
from test_live_events import item
from serein.compat.narratives import narrative_transaction
from serein.compat.germany.narrative_materials import narrative_preview_fingerprint
from serein.core import Store


def seed(client, settings):
    event = client.post('/api/fact-events/batch', json={'items': [item()]}).json()['items'][0]['item_id']
    result = client.post('/v1/tools/call', json={'name': 'write_scene', 'arguments': {
        'title': '雨声', 'content': '窗边一起听雨', 'cues': ['雨声'],
        'evidence_refs': item()['source_refs'],
    }})
    assert result.status_code == 200, result.text
    scene = re.search(r'\[scene_id:([^]]+)\]', result.json()['result'])[1]
    with narrative_transaction(settings.database, write=True) as rolls:
        result = rolls.publish(narrative_id='narrative_test', document=f'# 雨声\n\n## 第一人称叙事\n\n记下雨声。\n\n## 材料\n\n{event}\n{scene}\n',
            expected_revision=0, title='雨声', arc_key='arc:rain', source_event_ids=[event], source_scene_ids=[scene])
        assert result['status']=='created', result
    return event, scene


def proposal(client):
    current = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    response = client.post('/api/narrative-rolls/preview-input', json={
        'narrative_id': 'narrative_test', 'mode': 'edit', 'expected_revision': current['revision'],
        'expected_document_sha256': current['document_sha256']})
    assert response.status_code==200, response.text
    preview = response.json()
    body = '把这一天的雨声留下。'
    return preview, {'narrative_id': 'narrative_test', 'body': body,
        'expected_revision': preview['base_revision'], 'expected_document_sha256': preview['base_document_sha256'],
        'proposed_material_ids': preview['proposed_material_ids'],
        'expected_material_snapshot_sha256': preview['material_snapshot_sha256'],
        'preview_fingerprint': narrative_preview_fingerprint(narrative_id='narrative_test',
            revision=preview['base_revision'], document_sha256=preview['base_document_sha256'], body=body,
            material_snapshot_sha256_value=preview['material_snapshot_sha256'])}


def test_narrative_full_original_sources_sealed_save_and_retry(live):
    settings, client = live
    seed(client, settings)
    preview, payload = proposal(client)
    assert preview['writes_performed']==[]
    for kind in ('events', 'scenes'):
        source = preview['materials'][kind][0]
        assert source['source_mode']=='conversation'
        assert source['source_messages'][0]['content']==item()['source_refs'][0]['content']
    response = client.post('/api/narrative-rolls/save-body', json=payload)
    assert response.status_code==200, response.text
    assert response.json()['model_called'] is False
    assert response.json()['revision']==2
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').json()['body']==payload['body']
    assert client.post('/api/narrative-rolls/save-body', json=payload).status_code==409
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').json()['revision']==2


def test_source_change_after_preview_rejects_save_and_keeps_old_body(live):
    settings, client = live
    event, scene = seed(client, settings)
    preview, payload = proposal(client)
    evidence = client.post('/v1/tools/call', json={'name':'read_scene_evidence','arguments':{'scene_id':scene}}).json()['result']
    client.post('/v1/tools/call', json={'name':'unbind_scene_evidence','arguments':{'scene_id':scene,
        'evidence_ids':[evidence['evidence_refs'][0]['id']]}})
    response=client.post('/api/narrative-rolls/save-body', json=payload)
    assert response.status_code==409, response.text
    assert response.json()['reason']=='material_snapshot_changed'
    current=client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    assert current['revision']==1 and current['body']=='记下雨声。'


def test_arc_receipt_appends_without_changing_body_or_revision(live):
    settings, client=live
    event, scene=seed(client, settings)
    current=client.get('/api/narrative-arcs/cards').json()['items'][0]
    raw=client.post('/api/fact-events/read-many',json={'item_ids':[event]}).json()['items'][0]
    payload={'arc_key':current['arc_key'], 'expected_revision':current['revision'],
        'expected_document_sha256':current['document_sha256'],
        'events':[{'event_id':event,'fingerprint':raw['fingerprint']}]}
    response=client.post('/api/narrative-arcs/append-event-materials',json=payload)
    assert response.status_code==200,response.text
    assert response.json()['inserted']==1
    assert response.json()['authored_body_sha256_before']==response.json()['authored_body_sha256_after']
    assert client.post('/api/narrative-arcs/append-event-materials',json=payload).json()['idempotent']==1
    payload['expected_revision']=0
    assert client.post('/api/narrative-arcs/append-event-materials',json=payload).status_code==409


@pytest.mark.parametrize('legacy', [
    '2151802bad5c', 'ombre_2151802bad5c',
    'ombre_legacy-12345678-1234-5678-9abc-123456789abc',
])
def test_legacy_scene_material_survives_preview_and_sealed_save(live, legacy):
    settings, client = live
    event, _ = seed(client, settings)
    with Store(settings.database) as store:
        store.create(legacy, 'scene', '旧记忆', '保留原来的短 ID。')
    with narrative_transaction(settings.database, write=True) as rolls:
        result = rolls.publish(narrative_id='narrative_test',
            document=f'# 雨声\n\n## 第一人称叙事\n\n记下雨声。\n\n## 材料\n\n{event}\n{legacy}\n',
            expected_revision=1, title='雨声', arc_key='arc:rain',
            source_event_ids=[event], source_scene_ids=[legacy])
        assert result['status'] == 'updated', result
    before = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    preview, payload = proposal(client)
    assert preview['materials']['scenes'][0]['scene_id'] == legacy
    assert preview['proposed_material_ids']['scene_ids'] == [legacy]
    assert client.get('/api/narrative-rolls?narrative_id=narrative_test').json() == before
    response = client.post('/api/narrative-rolls/save-body', json=payload)
    assert response.status_code == 200, response.text
    current = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    assert current['linked_scene_ids'] == [legacy]


@pytest.mark.parametrize('invalid', [
    '../2151802bad5c', 'not-a-scene', '2151802bad5', '2151802bad5c/',
    'ombre_not-a-scene', 'ombre_2151802bad5', 'ombre_2151802bad5c/',
    'ombre_legacy-12345678-1234-5678-9abc-123456789ab',
    'ombre_legacy-12345678-1234-5678-9abc-123456789abc/extra',
])
def test_narrative_material_ids_still_reject_malformed_scene_ids(invalid):
    from serein.compat.germany.narrative_materials import normalize_material_ids
    with pytest.raises(ValueError, match='invalid_scene_id'):
        normalize_material_ids({'event_ids': [], 'scene_ids': [invalid],
            'diary_ids': [1], 'darkroom_ids': [], 'upload_ids': []})
