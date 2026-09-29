import pytest
from test_live_clients import live
from serein.core import Store
from serein.core.store import digest
from serein.compat.originals import Originals


def upload(client, **message):
    return client.post('/v1/originals/upload',json={'client':'test','conversation_id':'room-1',
        'messages':[{'message_id':'m1','role':'user','content':'A retained original.',**message}]})


def test_upload_exact_retry_conflict_and_unknown_time(live):
    settings,client=live
    first=upload(client)
    assert first.status_code==200,first.text
    key=first.json()['items'][0]['id']
    assert upload(client).json()['duplicate']==1
    assert upload(client,content='Rewritten text').status_code==409
    assert upload(client,role='system').status_code==400
    read=Originals(settings.database).source_message_read([key])['items'][0]
    assert read['content']=='A retained original.'
    with Store(settings.database) as store:
        assert store.conn.execute('select count(*) from raw_events').fetchone()[0]==1
        assert store.conn.execute("select json_extract(metadata_json,'$.original_time_unknown') from raw_events").fetchone()[0]==1
    client.headers.pop('Authorization')
    assert upload(client).status_code==401


def test_upload_batch_atomic_conflict_and_conversation_identity(live):
    settings,client=live
    upload(client)
    body={'client':'test','conversation_id':'room-1','messages':[
        {'message_id':'m2','role':'assistant','content':'Another original.'},
        {'message_id':'m1','role':'user','content':'Changed!'}]}
    assert client.post('/v1/originals/upload',json=body).status_code==409
    with Store(settings.database) as store:
        assert store.conn.execute('select count(*) from raw_events').fetchone()[0]==1
    body['conversation_id']='room-2'
    assert client.post('/v1/originals/upload',json=body).json()['inserted']==2


def test_delete_preview_confirmation_stale_receipt_and_retry_tombstone(live):
    settings,client=live
    key=upload(client).json()['items'][0]['id']
    preview=client.post('/v1/originals/delete-preview',json={'ids':[key]}).json()
    assert preview['can_delete']
    assert client.post('/v1/originals/delete',json={'items':preview['items'],'confirm':False}).status_code==400
    stale=[{**preview['items'][0],'sha256':'0'*64}]
    assert client.post('/v1/originals/delete',json={'items':stale,'confirm':True}).status_code==409
    deleted=client.post('/v1/originals/delete',json={'items':preview['items'],'confirm':True})
    assert deleted.status_code==200,deleted.text
    assert Originals(settings.database).source_message_read([key])['missing_ids']==[key]
    assert Originals(settings.database).source_message_search('retained')['items']==[]
    assert upload(client).status_code==409


def test_referenced_original_cannot_be_deleted(live):
    settings,client=live
    key=upload(client).json()['items'][0]['id']
    preview=client.post('/v1/originals/delete-preview',json={'ids':[key]}).json()
    with Store(settings.database) as store:
        store.add_source('raw:'+key,'A retained original.',metadata={})
    assert not client.post('/v1/originals/delete-preview',json={'ids':[key]}).json()['can_delete']
    assert client.post('/v1/originals/delete',json={'items':preview['items'],'confirm':True}).status_code==409
    assert Originals(settings.database).source_message_read([key])['items']


@pytest.mark.parametrize('message',[
    {'role':'tool'}, {'created_at':'yesterday'}, {'created_at':'2026-09-29T10:00:00'},
    {'message_id':''}, {'content':''}, {'unexpected':'field'},
])
def test_invalid_upload_is_rejected(live,message):
    _,client=live
    assert upload(client,**message).status_code==400
