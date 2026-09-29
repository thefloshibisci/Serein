"""Original lookup uses synthetic local transcripts only."""
import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from serein.application import Application
from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.bootstrap import initialize
from serein.compat.originals import Originals, original_timestamp
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings


@pytest.fixture
def settings(tmp_path):
    value = Settings(tmp_path/'memory.db', writable=True)
    initialize(value)
    return value


def seed(settings, number, text='雨天', role='user', time='2025-01-02T00:00:00+08:00',
         source='synthetic', session='a', conversation='', metadata=None):
    with Store(settings.database) as store:
        store.conn.execute('INSERT INTO raw_events '
            '(id,source,source_event_id,event_hash,role,text,created_at,ingested_at,session_id,conversation_id,metadata_json) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (number,source,'upstream-'+str(number),str(number),role,text,time,time,session,conversation,json.dumps(metadata or {})))


def ids(result):
    return [item['id'] for item in result['items']]


def test_default_limit_literal_matching_and_stable_pagination(settings):
    for n in range(1, 26):
        seed(settings,n,text=('很长的开头'*90 if n==25 else '')+'雨天 50%_完成',role='user' if n%2 else 'assistant')
    originals=Originals(settings.database)
    first=originals.source_message_search()
    assert len(first['items'])==10 and ids(first)==[f'raw:{n}' for n in range(25,15,-1)]
    seed(settings,26,time='2025-01-03T00:00:00+08:00')
    second=originals.source_message_search(before_id=first['next_before_id'])
    third=originals.source_message_search(before_id=second['next_before_id'])
    assert ids(second)+ids(third)==[f'raw:{n}' for n in range(15,0,-1)]
    assert third['next_before_id'] is None
    match=originals.source_message_search(query='%_',limit=50)
    assert len(match['items'])==25 and '%_' in match['items'][0]['preview']
    assert match['items'][0]['preview_offset']>0 and match['items'][0]['truncated']
    assert 'content' not in match['items'][0]
    assert not originals.source_message_search(query='50__')['items']
    assert all(item['metadata']['role']=='assistant' for item in originals.source_message_search(role='AI')['items'])


def test_dates_offsets_range_and_combined_filters(settings):
    seed(settings,1,time='2025-01-01T15:59:59Z')
    seed(settings,2,time='2025-01-01T16:00:00Z')
    seed(settings,3,time='2025-01-02T23:59:59+08:00',role='assistant')
    seed(settings,4,time='2025-01-02T16:00:00Z')
    seed(settings,5,time='2025-01-02 10:00:00',text='晴天')
    seed(settings,6,time='2025-01-01T11:00:00-05:00',role='assistant')
    search=Originals(settings.database).source_message_search
    assert ids(search(date='2025-01-02'))==['raw:3','raw:5','raw:6','raw:2']
    assert ids(search(date='2025-01-02',query='雨',role='user'))==['raw:2']
    assert ids(search(date='2025-01-02..2025-01-03',query='雨',role='ai'))==['raw:3','raw:6']
    assert ids(search(date='2025-01-03'))==['raw:4']


def test_bridge_utc_dates_sort_with_aware_and_legacy_times(settings):
    seed(settings,1,time='2025-01-01 16:00:00',source='haven_bridge_codex')
    seed(settings,2,time='2025-01-02 01:00:00')
    seed(settings,3,time='2025-01-01T18:00:00Z')
    search=Originals(settings.database).source_message_search
    assert ids(search(date='2025-01-02'))==['raw:3','raw:2','raw:1']
    assert not search(date='2025-01-01')['items']


@pytest.mark.parametrize('source,value,expected', [
    ('haven_bridge_codex','2026-09-27 08:39:21','2026-09-27T16:39:21+08:00'),
    ('synthetic','2026-09-27 08:39:21','2026-09-27T08:39:21+08:00'),
    ('haven_bridge_codex','2026-09-27T08:39:21Z','2026-09-27T16:39:21+08:00'),
    ('haven_bridge_codex','2026-09-27T16:39:21+08:00','2026-09-27T16:39:21+08:00'),
    ('synthetic','',''),
    ('synthetic','unknown','unknown'),
])
def test_original_timestamp_preserves_offsets_and_unknown_dates(source,value,expected):
    assert original_timestamp({'source':source,'created_at':value}, 'Asia/Shanghai')==expected


@pytest.mark.parametrize('arguments',[
    {'limit':0},{'limit':51},{'limit':1.5},{'limit':True},{'role':'system'}, {'date':None}, {'query':[]},
    {'date':'2025-02-30'},{'date':'2025-1-2'},{'date':'2025-01-03..2025-01-02'},
    {'date':'2025-01-01..'},{'date':'2025-01-01..2025-01-02..2025-01-03'},
    {'before_id':'upstream-7'},{'before_id':'raw:999999999999999999999'},
])
def test_invalid_search_arguments(settings,arguments):
    with pytest.raises(ValueError):Originals(settings.database).source_message_search(**arguments)


def test_read_neighbors_are_readable_same_conversation_and_deduplicated(settings):
    seed(settings,1)
    seed(settings,2,role='system')
    seed(settings,3,role='assistant',text='完整原话'*2000)
    seed(settings,4,session='other')
    seed(settings,5)
    seed(settings,6,source='other-source')
    seed(settings,7,metadata={'draft':True})
    seed(settings,8,metadata={'discarded':True})
    seed(settings,9,role='assistant')
    seed(settings,10,conversation='different-conversation')
    seed(settings,11)
    seed(settings,12,source='error')
    seed(settings,13,role='trace')
    seed(settings,14,session='')
    seed(settings,15,session='')
    originals=Originals(settings.database)
    before=settings.database.read_bytes()
    result=originals.source_message_read(['raw:3','5','raw:5','raw:9999','raw:2','raw:7','raw:8','raw:12','raw:13'],1,1)
    assert ids(result)==['raw:1','raw:3','raw:5','raw:9']
    assert [i['id'] for i in result['items'] if i['is_target']]==['raw:3','raw:5']
    assert len(result['items'][1]['content'])==8000
    assert result['missing_ids']==['raw:9999']
    assert result['excluded_ids']==['raw:2','raw:7','raw:8','raw:12','raw:13']
    assert not set(result['excluded_ids']) & set(ids(originals.source_message_search(limit=50)))
    unknown=originals.source_message_read(['raw:14'],1,1)
    assert ids(unknown)==['raw:14'] and unknown['context_unavailable_ids']==['raw:14']
    assert settings.database.read_bytes()==before
    with pytest.raises(ValueError):originals.source_message_read(['raw:3'],neighbor_after=4)
    with pytest.raises(ValueError):originals.source_message_read(['upstream-3'])


def test_source_evidence_ids_do_not_guess_upstream_ids_or_context(settings):
    seed(settings,1,text='Other original',session='other')
    with Store(settings.database) as store:
        store.create('event','event','Event','Body')
        source=store.add_source('source-key','Actual original',metadata={'message_id':'1','session_id':'a'})
        store.bind('event',source)
    originals=Originals(settings.database)
    key='source:'+source
    result=originals.source_message_read([key],1,1)
    assert ids(result)==[key] and result['items'][0]['content']=='Actual original'
    assert result['context_unavailable_ids']==[key]
    with Store(settings.database) as store:store.set_lifecycle('event','deleted')
    assert originals.source_message_read([key])['missing_ids']==[key]


def test_optional_tools_hot_switch_mcp_http_whitelist_and_read_only(settings):
    seed(settings,1)
    server=create_server(Application(settings))
    names=lambda:{t.name for t in asyncio.run(server.list_tools())}
    tool_names={'source_message_search','source_message_read'}
    restricted=create_server(Application(replace(settings,mcp_tools=[*tool_names, 'read_source_messages', 'list_source_messages'])))
    assert not tool_names & names()
    with TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'}) as client:
        assert client.get('/v1/settings').json()['features']['originals'] is False
        assert client.post('/v1/extensions/source_message_search',json={}).status_code==404
        assert client.patch('/v1/settings',json={'features':{'originals':True}}).status_code==200
        assert tool_names <= names()
        assert {t.name for t in asyncio.run(restricted.list_tools())}==tool_names
        for tool in asyncio.run(server.list_tools()):
            if tool.name in tool_names:assert tool.annotations.readOnlyHint is True
            if tool.name=='source_message_search':assert tool.inputSchema['properties']['limit']['default']==10
        assert ids(client.post('/v1/extensions/source_message_search',json={}).json())==['raw:1']
        assert client.post('/v1/extensions/source_message_search',json={'limit':0}).status_code==400
        assert client.post('/v1/extensions/source_message_read',json={'ids':['raw:1']}).json()['items'][0]['content']=='雨天'
        read_only=create_server(Application(replace(settings,writable=False)))
        assert tool_names <= {t.name for t in asyncio.run(read_only.list_tools())}
        # Actual MCP invocation succeeds, not just tool registration.
        result=asyncio.run(server.call_tool('source_message_search',{}))
        assert ids(json.loads(result[0].text))==['raw:1']
        result=asyncio.run(server.call_tool('source_message_read',{'ids':['raw:1']}))
        assert json.loads(result[0].text)['items'][0]['content']=='雨天'
        client.patch('/v1/settings',json={'features':{'originals':False}}).raise_for_status()
        assert not tool_names & names()
        assert asyncio.run(restricted.list_tools())==[]
        for name in tool_names:
            with pytest.raises(Exception):asyncio.run(server.call_tool(name,{'ids':['raw:1']} if name.endswith('read') else {}))
            assert client.post('/v1/extensions/'+name,json={}).status_code==404
        save_settings(settings.database,{'features':{'originals':True}})
        assert ids(Application(settings).contributions.tools['source_message_search']())==['raw:1']
