import asyncio
from test_live_clients import live
from test_live_relations import create
from serein.core.store import Store
from serein.core.personal import Personal
from serein.api.mcp import create_server
from serein.application import Application
from serein.api.surface import SELF_USE_TOOLS


def test_personal_save_reload_conflict_and_old_browser_cannot_resurrect(live):
    settings,client=live
    key=create(client,'收藏的一幕','我们一起听雨。')
    with Store(settings.database,read_only=True) as store:
        revision=store.read(key)['revision']
        jobs=store.conn.execute('SELECT count(*) FROM scene_jobs').fetchone()[0]
    favorite={'scope':'favorite','key':key,'document_id':key,'value':{'favorite':True}}
    saved=client.post('/api/personal',json=favorite).json()
    assert saved['revision']==1
    note={'scope':'annotation','key':'note-a','document_id':key,'value':{'content':'后来再读时的感受','role':'user','author':'用户'}}
    assert client.post('/api/personal',json=note).status_code==200
    projection=client.post('/api/serein/memory-projection',json={}).json()['scenes'][0]
    assert projection['favorite'] and projection['annotations'][0]['content']=='后来再读时的感受'
    assert Personal(settings.database).read_favorites(limit=1)['items'][0]['annotations'][0]['content']=='后来再读时的感受'
    off={**favorite,'expected_revision':1,'value':{'favorite':False}}
    assert client.post('/api/personal',json=off).status_code==200
    assert client.post('/api/personal',json={**favorite,'expected_revision':1}).status_code==409
    assert client.post('/api/personal',json={**note,'expected_revision':1,'deleted':True}).status_code==200
    assert client.post('/api/personal/import',json={'records':[favorite,note]}).json()['inserted']==0
    assert Personal(settings.database).read_favorites()['items']==[]
    assert client.get('/api/personal?scope=annotation').json()['items']==[]
    with Store(settings.database,read_only=True) as store:
        assert store.read(key)['revision']==revision
        assert store.conn.execute('SELECT count(*) FROM scene_jobs').fetchone()[0]==jobs


def test_favorites_pagination_and_deleted_archived_access(live):
    settings,client=live;personal=Personal(settings.database)
    keys=[create(client,str(n),'听雨'+str(n)) for n in range(3)]
    for key in keys:personal.save('favorite',key,{'favorite':True},document_id=key)
    first=personal.read_favorites(limit=1)
    assert first['has_more'] and first['items'][0]['id']==keys[2]
    assert personal.read_favorites(limit=1,offset=1)['items'][0]['id']==keys[1]
    with Store(settings.database) as store:
        store.set_lifecycle(keys[2],'deleted');store.set_lifecycle(keys[1],'archived')
    assert [x['id'] for x in personal.read_favorites()['items']]==[keys[0]]
    assert len(personal.read_favorites(include_archived=True)['items'])==2


def test_review_and_simulation_survive_new_store_and_remain_separate(live):
    settings,client=live
    review={'scope':'recall_review','key':'hook-42','value':{'verdict':'correct','query':'听雨','candidateReviews':{'scene-a':'core'},'injected_body':'must not persist'}}
    simulation={'scope':'recall_simulation','key':'simulation-a','value':{'id':'simulation-a','query':'听雨','expectedAction':'recall','source':'manual_simulation'}}
    assert client.post('/api/personal/import',json={'records':[review,simulation]}).json()['inserted']==2
    assert client.post('/api/personal/import',json={'records':[review,simulation]}).json()['inserted']==0
    records=Personal(settings.database).list('recall_review')['items']
    assert records[0]['value']['candidateReviews']=={'scene-a':'core'}
    assert 'injected_body' not in records[0]['value']
    assert Personal(settings.database).list('recall_simulation')['items'][0]['value']['source']=='manual_simulation'


def test_private_mcp_catalog_and_favorite_limit(live):
    settings,client=live
    from serein.deployment import save_settings
    save_settings(settings.database,{'features':{'favorites':True}})
    server=create_server(Application(settings),private=True)
    tools=asyncio.run(server.list_tools())
    assert {t.name for t in tools}==set(SELF_USE_TOOLS) | {
        'ob_archive'}
    favorite=next(t for t in tools if t.name=='read_favorites')
    assert favorite.inputSchema['properties']['limit']['default']==5
    assert favorite.inputSchema['properties']['offset']['default']==0


def test_optional_favorite_tool_filters_paginates_and_never_records_recall(live):
    import json
    import pytest
    from dataclasses import replace
    settings,client=live;personal=Personal(settings.database)
    server=create_server(Application(settings))
    restricted=create_server(Application(replace(settings,mcp_tools=['read_favorites'])))
    assert 'read_favorites' not in {t.name for t in asyncio.run(server.list_tools())}
    assert client.post('/v1/extensions/read_favorites',json={}).status_code==404
    with Store(settings.database) as store:
        for i in range(12):store.create(f'event_{i:02}','event',f'Favorite {i}',f'Full original body {i}')
        store.create('scene_saved','scene','Saved scene','Scene body')
        store.create('narrative_saved','narrative','Saved volume','Narrative body')
        source=store.add_source('message:1','Exact source');store.bind('event_01',source)
    for key in [*[f'event_{i:02}' for i in range(12)],'scene_saved','narrative_saved']:
        personal.save('favorite',key,{'favorite':True},document_id=key)
    with Store(settings.database) as store:
        store.set_lifecycle('event_00','archived')
        store.set_lifecycle('event_10','deleted')
        store.set_lifecycle('event_11','superseded')
    client.patch('/v1/settings',json={'features':{'favorites':True}}).raise_for_status()
    tools=asyncio.run(server.list_tools());tool=next(t for t in tools if t.name=='read_favorites')
    assert tool.annotations.readOnlyHint and tool.inputSchema['properties']['limit']['default']==5
    assert tool.inputSchema['properties']['include_archived']['default'] is False
    assert [t.name for t in asyncio.run(restricted.list_tools())]==['read_favorites']
    with Store(settings.database,read_only=True) as store:before='\n'.join(store.conn.iterdump())
    first=client.post('/v1/extensions/read_favorites',json={}).json()
    assert len(first['items'])==5 and first['has_more'] and first['injected'] is False
    second=client.post('/v1/extensions/read_favorites',json={'offset':first['next_offset']}).json()
    ids=[item['id'] for item in first['items']+second['items']]
    assert len(set(ids))==10 and 'scene_saved' in ids and 'event_00' not in ids
    archived=client.post('/v1/extensions/read_favorites',json={'include_archived':True,'limit':100}).json()
    assert len(archived['items'])==11 and any(item['id']=='event_00' for item in archived['items'])
    assert not {'event_10','event_11','narrative_saved'} & set(ids)
    favorite_result=asyncio.run(server.call_tool('read_favorites',{'kind':'scene'}))
    assert len(favorite_result)==1 and '[favorites]' in favorite_result[0].text
    assert 'body:\nScene body' in favorite_result[0].text
    assert next(t for t in asyncio.run(server.list_tools()) if t.name=='read_favorites').outputSchema is None
    events=client.post('/v1/extensions/read_favorites',json={'kind':'event','include_archived':False,'with_evidence':True,'limit':100}).json()
    assert len(events['items'])==9 and all(item['kind']=='event' for item in events['items'])
    assert next(item for item in events['items'] if item['id']=='event_01')['evidence']
    for args in [{'kind':'narrative'},{'limit':0},{'limit':True},{'offset':-1},{'with_evidence':'yes'}]:
        assert client.post('/v1/extensions/read_favorites',json=args).status_code==400
    read_only=create_server(Application(replace(settings,writable=False)))
    assert 'id: scene:scene_saved' in asyncio.run(read_only.call_tool('read_favorites',{'kind':'scene'}))[0].text
    with Store(settings.database,read_only=True) as store:assert '\n'.join(store.conn.iterdump())==before
    client.patch('/v1/settings',json={'features':{'favorites':False}}).raise_for_status()
    assert asyncio.run(restricted.list_tools())==[]
    assert client.post('/v1/extensions/read_favorites',json={}).status_code==404
    with pytest.raises(Exception):asyncio.run(server.call_tool('read_favorites',{}))
    assert personal.list('favorite')['items']  # Disabling reads never removes saved marks.


def test_existing_live_event_favorites_use_state_tool_without_touching_event(live):
    import json
    import pytest
    from test_live_events import item
    settings,client=live
    key=client.post('/api/fact-events/settlement',json={'operation_id':'favorite-fixture','items':[item()]}).json()['items'][0]['item_id']
    client.patch('/v1/settings',json={'features':{'favorites':True}}).raise_for_status()
    server=create_server(Application(settings))
    with Store(settings.database,read_only=True) as store:
        before=store.read(key);events=[tuple(row) for row in store.conn.execute('SELECT * FROM fact_events')]
        jobs=store.conn.execute('SELECT count(*) FROM index_outbox').fetchone()[0]
    args={'operation_id':'live-favorite','document_id':key,'expected_revision':before['revision'],'favorite':True}
    def change(arguments):
        result=asyncio.run(server.call_tool('set_memory_state',arguments))
        return result[1] if isinstance(result,tuple) else json.loads(result[0].text)
    saved=change(args)
    assert saved['favorite'] is True and saved['revision']==before['revision']
    assert client.get('/api/personal?scope=favorite').json()['items'][0]['value']['favorite'] is True
    args.update(operation_id='live-unfavorite',favorite=False)
    assert change(args)['favorite'] is False
    with Store(settings.database,read_only=True) as store:
        assert store.read(key)==before
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM fact_events')]==events
        assert store.conn.execute('SELECT count(*) FROM index_outbox').fetchone()[0]==jobs
    # Combining a legacy Event body-state change must still use its canonical writer.
    with pytest.raises(Exception):asyncio.run(server.call_tool('set_memory_state',{**args,'operation_id':'unsafe-mixed','lifecycle':'archived'}))
    client.patch('/v1/settings',json={'features':{'favorites':False}}).raise_for_status()
    with pytest.raises(Exception):asyncio.run(server.call_tool('set_memory_state',{**args,'operation_id':'disabled-write','favorite':True}))
