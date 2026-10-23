"""Synthetic schema failures give the next model attempt a specific repair."""
import asyncio
import copy
import json

import pytest

from serein.core.store import Store, encode
from serein.deployment import save_settings
from serein.extensions import pipeline as p, pipeline_latest as latest
from test_public_features import settings, ingest, output_for


def context_request():
    request={'role':'event_writer','component':{'track_ids':['synthetic-track'],
        'messages':[{'id':11},{'id':12}]}}
    query={'track_id':'synthetic-track','before_message_id':11,'reason':'missing_subject'}
    return request,query


@pytest.mark.parametrize('kind,expected', [
    ('used','已补读一次'), ('mixed','顶层只允许 context_request'),
    ('object','必须是对象'), ('fields',"missing=['before_message_id', 'reason']"),
    ('track',"allowed=['synthetic-track']"), ('boundary','expected=11, received=12'),
    ('reason','missing_prior_claim'), ('string_id',"expected=11, received='11'")])
def test_context_errors_identify_the_actual_failed_condition(kind,expected):
    request,query=context_request()
    output={'context_request':query}
    if kind=='used':request['context_read']=True
    if kind=='mixed':output.update(evidence_sufficient=False,title='',event_draft='',
        kept_details=[],discarded_details=[],self_review={})
    if kind=='object':output['context_request']=[]
    if kind=='fields':output['context_request']={'track_id':'synthetic-track'}
    if kind=='track':query['track_id']='foreign'
    if kind=='boundary':query['before_message_id']=12
    if kind=='reason':query['reason']='more_context'
    if kind=='string_id':query['before_message_id']='11'
    original=copy.deepcopy(output)
    with pytest.raises(ValueError) as error:
        p.validate(request,output)
    assert expected in str(error.value)
    if kind=='mixed':
        assert all(key in str(error.value) for key in output if key!='context_request')
    assert output==original


def test_boolean_cannot_stand_in_for_the_context_boundary_id():
    request,query=context_request()
    request['component']['messages']=[{'id':1}]
    query['before_message_id']=True
    with pytest.raises(ValueError,match='expected=1, received=True'):
        p.validate(request,{'context_request':query})


def test_empty_writer_skeleton_is_valid_without_context_but_nonempty_fields_are_identified():
    result={'evidence_sufficient':False,'title':'','event_draft':'',
        'kept_details':[],'discarded_details':[],'self_review':{}}
    assert latest.validate_event_writer_result(result)==[]
    for key,value in [('title','A title'),('event_draft','Some prose'),
                      ('kept_details',['A detail']),('discarded_details',['An explanation'])]:
        errors=latest.validate_event_writer_result({**result,key:value})
        assert len(errors)==1
        assert f"非空字段=['{key}']" in errors[0]


def configure_api(settings):
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic',
        'base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{role:'local' for role in p.ROLES},'pipeline':{'execution_mode':'api'}})


def current_request(settings):
    with Store(settings.database,read_only=True) as store:
        return json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs '
            'WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])


def test_mixed_writer_context_response_corrects_then_reads_once_and_saves(settings,monkeypatch):
    ingest(settings);configure_api(settings)
    calls=[];reads=[]
    original=p.extend_context
    def read(database,component,query):
        reads.append(query)
        return original(database,component,query)
    monkeypatch.setattr(p,'extend_context',read)
    async def complete(model,payload):
        request=current_request(settings)
        role=request['role'];calls.append(role)
        output=output_for(role,request)
        if role=='event_writer' and not request.get('context_read'):
            query={'track_id':request['component']['track_ids'][0],
                'before_message_id':min(m['id'] for m in request['component']['messages']),
                'reason':'missing_subject'}
            if calls.count(role)==1:
                output={'context_request':query,'evidence_sufficient':False,
                    'kept_details':[],'discarded_details':[],'self_review':{},'title':'','event_draft':''}
            else:
                assert '顶层只允许 context_request' in payload['messages'][1]['content']
                assert 'extra=' in payload['messages'][1]['content']
                output={'context_request':query}
        return {'choices':[{'message':{'content':json.dumps(output)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert result['events']==1 and result['processed_originals']==2
    assert calls==['track_router','event_curator','event_writer','event_writer','event_writer']
    assert len(reads)==1
    with Store(settings.database,read_only=True) as store:
        errors=[row[0] for row in store.conn.execute("SELECT error FROM pipeline_attempts WHERE error!=''")]
        assert len(errors)==1 and '顶层只允许 context_request' in errors[0]
        assert store.conn.execute("SELECT count(*) FROM pipeline_batches WHERE status='paused_failure'").fetchone()[0]==0


def test_curator_missing_dispositions_repairs_using_unit_root_keys(settings,monkeypatch):
    ingest(settings);configure_api(settings)
    calls=[]
    async def complete(model,payload):
        request=current_request(settings);role=request['role'];calls.append(role)
        output=output_for(role,request)
        if role=='event_curator':
            if calls.count(role)==1:
                output.pop('skip_unit_roots');output.pop('defer_unit_roots')
            else:
                assert "missing=['defer_unit_roots', 'skip_unit_roots']" in payload['messages'][1]['content']
        return {'choices':[{'message':{'content':json.dumps(output)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert result['events']==1 and result['processed_originals']==2
    assert calls==['track_router','event_curator','event_curator','event_writer']


def test_needs_repair_is_not_reported_as_a_missing_paused_batch(settings):
    p.initialize(settings.database)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES (?,?,?,'needs_repair')",
            ('synthetic-held','scope',encode({'contract':p.CONTRACT,'runtime_revision':p.runtime_revision()})))
    with pytest.raises(ValueError,match='needs_repair.*不能使用暂停批次重试'):
        p.retry_batch(settings.database,'synthetic-held')
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT status FROM pipeline_batches WHERE id='synthetic-held'").fetchone()[0]=='needs_repair'


@pytest.mark.parametrize('outdated',[False,True])
def test_existing_repair_hold_gets_runtime_guidance_only_when_revision_differs(settings,outdated):
    p.initialize(settings.database)
    data=encode({'contract':p.CONTRACT,'runtime_revision':'older-synthetic-code' if outdated else p.runtime_revision()})
    detail=encode({'status':'needs_repair','reason':'Frozen predecessor frame disagrees'})
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status,result_json) VALUES (?,?,?,'needs_repair',?)",
            ('synthetic-held','scope',data,detail))
        store.conn.execute('INSERT INTO pipeline_job_failures VALUES (?,?,?)',
            ('synthetic-held:event_writer:0',3,'old failure'))
    p.initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute("SELECT * FROM pipeline_batches WHERE id='synthetic-held'").fetchone()
        result=json.loads(row['result_json'])
        assert row['input_json']==data and row['status']=='needs_repair'
        assert store.conn.execute('SELECT failures FROM pipeline_job_failures').fetchone()[0]==3
        if outdated:
            assert result['repair_kind']=='runtime_contract'
            assert result['previous_result']==json.loads(detail)
        else:
            assert row['result_json']==detail
        once=row['result_json']
    p.initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT result_json FROM pipeline_batches WHERE id='synthetic-held'").fetchone()[0]==once
