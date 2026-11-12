"""Synthetic fixtures for retained work, explicit adoption and atomic closure."""
import asyncio
import json

import pytest

from test_public_features import settings,ingest,output_for,synthetic_runner
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.extensions import pipeline_pending as pending


async def insufficient(role,request):
    return {'evidence_sufficient':False,'self_review':{}} if role=='event_writer' else output_for(role,request)


@pytest.mark.parametrize('adopt',[False,True])
def test_reserved_target_reads_predecessor_before_writer_without_forcing_adoption(settings,adopt):
    ingest(settings)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    ingest(settings,2)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=insufficient))['pending']==2
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    ingest(settings,3);calls=[]
    async def run(role,request):
        calls.append((role,request.get('context_read',False)))
        out=output_for(role,request)
        if role=='event_curator' and request.get('context_read'):
            assert request['component']['pending_material_read']['id']==item['id']
            assert '旧 Event 的前置待办预留' in request['prompt']
            assert 'reserved_event_ids' in request['prompt']
            assert all(base in request['prompt'] for base in out['events'][0]['base_event_ids'])
            assert '不要返回未承接却继续修改上述 Event 的原方案' in request['prompt']
            if adopt:
                out['adopt_pending_material_ids']=[item['id']]
                out['events'][0]['owned_unit_roots']=[u['unit_root_message_id'] for u in request['component']['memberships']]
        return out
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=run))
    assert ('event_curator',True) in calls
    assert result['status']=='processed' and result['events']==int(adopt)
    assert result['processed_originals']==(4 if adopt else 0)
    if not adopt:assert not any(role=='event_writer' for role,_ in calls)
    with Store(settings.database) as store:
        old=next(i for i in pending.entries(store.conn) if i['id']==item['id'])
        assert old['status']==('resolved' if adopt else 'open')
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==6


def test_dependency_deferral_keeps_unrelated_create_and_final_race_guard(settings):
    item=retained(settings)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_jobs SET output_json=? WHERE batch_id=? AND role='event_curator:0'",
            (json.dumps({'events':[{'base_event_ids':['reserved']}]}),item['batch_id']))
    component={'messages':[{'id':3},{'id':4}],'adopted_pending_ids':[]}
    plan={'events':[{'base_event_ids':['reserved'],'source_message_ids':[1,3]},
                    {'base_event_ids':[],'source_message_ids':[4]}],
          'defer_source_message_ids':[],'skip_source_message_ids':[]}
    actual=pending.defer_blocked_targets(settings.database,'new',component,plan)
    assert actual['events']==[plan['events'][1]] and actual['defer_source_message_ids']==[3]
    assert actual['host_deferrals'][0]['pending_material_ids']==[item['id']]
    assert plan['defer_source_message_ids']==[]
    shared={**plan,'events':[plan['events'][0],{'base_event_ids':[],'source_message_ids':[3,4]}]}
    shared_result=pending.defer_blocked_targets(settings.database,'new',component,shared)
    assert shared_result['events']==[] and shared_result['defer_source_message_ids']==[3,4]
    from serein.compat.germany.fact_events import FactEventSettlementBlockedError
    with Store(settings.database) as store:
        with pytest.raises(FactEventSettlementBlockedError):
            pending.assert_targets_available(store.conn,'new',[(component,plan,[])])
        pending.assert_targets_available(store.conn,'new',[(component,actual,[])])


@pytest.mark.parametrize('legacy_replacement',[False,True])
@pytest.mark.parametrize('retained_count',[2,12])
def test_replan_unrouted_pending_builds_complete_event_input(settings,monkeypatch,legacy_replacement,retained_count):
    from test_route_scope_holds import seed,api
    seed(settings,count=6)
    api(settings,monkeypatch,lambda request:pytest.fail('Partial route cache must be held'))
    p.initialize(settings.database)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_routes VALUES (1,'{}')")
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database) as store:
        item=pending.entries(store.conn)[0]
        assert item['kind']=='unrouted' and item['batch_status']=='needs_repair'
        item['source_ids']=item['source_ids'][:retained_count]
        store.conn.execute('UPDATE pipeline_pending_materials SET source_ids_json=? WHERE id=?',
                           (json.dumps(item['source_ids']),item['id']))
        old=item['batch_id'];assert old.startswith('route:')
        if legacy_replacement:
            # A prior recovery version changed the prefix but omitted the
            # settlement inputs. Upgrade recovery must repair that shape too.
            store.conn.execute('UPDATE pipeline_batches SET id=? WHERE id=?',('pipeline:old-recovery',old))
            old='pipeline:old-recovery'
        originals=[tuple(row) for row in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]
        jobs=[tuple(row) for row in store.conn.execute('SELECT * FROM pipeline_jobs ORDER BY rowid')]
    resumed=pending.resume(settings.database,item['id'],replan=True)
    assert resumed['batch_id'].startswith('pipeline:')
    with Store(settings.database) as store:
        fresh=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(resumed['batch_id'],)).fetchone()[0])
        assert [m['id'] for m in fresh['messages']]==item['source_ids']
        assert [m['id'] for m in fresh['parked']]==list(range(retained_count+1,13))
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(old,)).fetchone()[0]=='superseded_repair'
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]==originals
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM pipeline_jobs ORDER BY rowid')]==jobs
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    assert result['events']==1 and result['processed_originals']==retained_count
    with Store(settings.database) as store:
        assert pending.entries(store.conn)[0]['status']=='resolved'
        assert [row[0] for row in store.conn.execute('SELECT raw_id FROM raw_processing ORDER BY raw_id')]==item['source_ids']


def test_curator_unknown_field_is_retained_without_writing(settings):
    ingest(settings)
    calls=[]
    async def extra_field(role,request):
        calls.append(role)
        output=output_for(role,request)
        if role=='event_curator':output['events'][0]['primary_activity_details']='Synthetic extra summary'
        return output
    for _ in range(2):
        with pytest.raises(ValueError,match="unexpected=\\['primary_activity_details'\\]"):
            asyncio.run(p.advance(settings.database,include_recent=True,runner=extra_field))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=extra_field))['status']=='retained'
    before=list(calls)
    asyncio.run(p.advance(settings.database,include_recent=True,runner=extra_field))
    assert calls==before and 'event_writer' not in calls
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        item=pending.entries(store.conn)[0]
    assert 'Synthetic extra summary' in pending.detail(settings.database,item['id'])['last_error']['output_text']


def retained(settings):
    ingest(settings)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=insufficient))
    assert result['events']==0 and result['pending']==2
    with Store(settings.database) as store:return pending.entries(store.conn)[0]


def adopting(*,background=False,writer_fails=False):
    async def run(role,request):
        if role=='event_curator':
            component=request['component']
            if not request.get('context_read'):
                assert 'Book club plan 1' not in request['prompt']
                return {'context_request':{'pending_material_id':component['pending_materials'][0]['pending_material_id'],'reason':'continue_pending'}}
            output=output_for(role,request)
            if not background:
                output['adopt_pending_material_ids']=[component['pending_material_read']['id']]
                output['events'][0]['owned_unit_roots']=[u['unit_root_message_id'] for u in component['memberships']]
            return output
        if role=='event_writer' and writer_fails:return {'evidence_sufficient':False,'self_review':{}}
        return output_for(role,request)
    return run


def test_insufficient_is_reserved_and_resume_reuses_the_cut(settings):
    item=retained(settings)
    assert item['kind']=='writing'
    async def forbidden(*args):pytest.fail('Unchanged retained work was called automatically')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'
    p.initialize(settings.database)
    with Store(settings.database) as store:
        data=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches').fetchone()[0])
        assert 'components' in data
        assert not json.loads(store.conn.execute("SELECT request_json FROM pipeline_jobs WHERE role LIKE 'event_writer:%'").fetchone()[0]).get('task_snapshot_compacted')
    pending.resume(settings.database,item['id'])
    calls=[]
    async def good(role,request):calls.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=good))['events']==1
    assert calls==['event_writer']
    with Store(settings.database) as store:assert pending.entries(store.conn)[0]['status']=='resolved'


def test_prompt_limit_retains_once_without_model_calls(settings):
    ingest(settings);save_settings(settings.database,{'pipeline':{'max_prompt_chars':1}})
    async def forbidden(*args):pytest.fail('Oversized prompt reached model')
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))
    assert result['status']=='retained'
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'
    with Store(settings.database) as store:
        item=pending.entries(store.conn)[0]
        assert item['last_attempt']['output_chars']==0 and item['kind']=='unrouted'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_resume_keeps_old_cut_even_after_an_independent_new_event(settings):
    item=retained(settings);ingest(settings,2)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    pending.resume(settings.database,item['id']);calls=[]
    async def good(role,request):calls.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=good))['events']==1
    assert calls==['event_writer']


def test_retained_originals_are_short_references_in_router_prompt(settings):
    item=retained(settings);ingest(settings,2)
    calls=[]
    async def good(role,request):
        if role=='track_router':
            calls.append(role)
            assert 'Book club plan 1' not in request['prompt']
            assert 'Agreed to plan 1' not in request['prompt']
            assert 'pending_material_id' in request['prompt']
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=good))['events']==1
    assert calls==['track_router']


def test_partial_batch_resume_never_rewrites_the_successful_event(settings):
    ingest(settings);ingest(settings,2)
    async def run(role,request):
        out=output_for(role,request)
        if role=='track_router':
            for index,row in enumerate(out['message_assignments']):row['primary_track_ref']='new:1' if index<2 else 'new:2'
            out['track_updates'].append({**out['track_updates'][0],'track_ref':'new:2','subject':'Independent synthetic plan'})
        if role=='event_writer' and min(m['id'] for m in request['messages'])==1:return {'evidence_sufficient':False,'self_review':{}}
        return out
    # Keep the two pairs within a single transport window.
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:02:00Z' WHERE id=3")
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:03:00Z' WHERE id=4")
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=run))['events']==1
    with Store(settings.database) as store:
        item=pending.entries(store.conn)[0]
        assert item['source_ids']==[1,2]
        existing=dict(store.conn.execute('SELECT item_id,body FROM fact_events').fetchone())
    pending.resume(settings.database,item['id']);calls=[]
    async def good(role,request):calls.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=good))['events']==1
    assert calls==['event_writer']
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==2
        assert store.conn.execute('SELECT body FROM fact_events WHERE item_id=?',(existing['item_id'],)).fetchone()[0]==existing['body']


@pytest.mark.parametrize('background',[False,True])
def test_future_event_closes_only_explicit_whole_adoption(settings,background):
    item=retained(settings);ingest(settings,2)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting(background=background)))
    assert result['events']==1 and result['pending']==0
    with Store(settings.database) as store:
        saved=next(i for i in pending.entries(store.conn) if i['id']==item['id'])
        assert saved['status']==('open' if background else 'resolved')
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==(2 if background else 4)
        if not background:assert saved['resolved_event_ids']


def test_failed_adopting_writer_leaves_old_pending_open(settings):
    item=retained(settings);ingest(settings,2)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting(writer_fails=True)))
    assert result['events']==0
    with Store(settings.database) as store:
        assert next(i for i in pending.entries(store.conn) if i['id']==item['id'])['status']=='open'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        latest=pending.entries(store.conn)[0]['id']
    pending.resume(settings.database,latest)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    assert result['events']==1
    with Store(settings.database) as store:
        assert all(i['status']=='resolved' for i in pending.entries(store.conn))


def test_partial_or_fabricated_adoption_is_rejected(settings):
    item=retained(settings);ingest(settings,2)
    p.initialize(settings.database);batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json']);routed=asyncio.run(p.route_batch(settings.database,batch,data,synthetic_runner))
    data=p.save_routing_snapshot(settings.database,batch,data,routed)
    component=data['components'][0];component['pending_materials']=pending.catalog(settings.database,component)
    with pytest.raises(ValueError):pending.adopted_component(component,{'adopt_pending_material_ids':[item['id']]})
    component=pending.read_material(settings.database,component,item['id'])
    adopted=pending.adopted_component(component,{'adopt_pending_material_ids':[item['id']]})
    with pytest.raises(ValueError,match='整份承接'):
        pending.check_adoption(adopted,{'events':[{'source_message_ids':[1,3,4]}],'skip_source_message_ids':[],'defer_source_message_ids':[2]})


def test_final_save_failure_does_not_consume_pending(settings,monkeypatch):
    item=retained(settings);ingest(settings,2)
    from serein.compat.events import Events
    def fail(*args,**kwargs):raise RuntimeError('synthetic storage failure')
    monkeypatch.setattr(Events,'settle',fail)
    with pytest.raises(RuntimeError):asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting()))
    with Store(settings.database) as store:
        assert next(i for i in pending.entries(store.conn) if i['id']==item['id'])['status']=='open'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_transaction_rolls_back_adoption_and_event_together(settings,monkeypatch):
    item=retained(settings);ingest(settings,2)
    original=pending.finish_adoptions
    def fail(conn,*args):original(conn,*args);raise RuntimeError('synthetic before-commit failure')
    monkeypatch.setattr(pending,'finish_adoptions',fail)
    with pytest.raises(RuntimeError):asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting()))
    with Store(settings.database) as store:
        assert next(i for i in pending.entries(store.conn) if i['id']==item['id'])['status']=='open'
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_latest_bad_output_is_accessible_without_becoming_an_event(settings):
    ingest(settings)
    async def malformed(role,request):
        return {'bad_output':'SYNTHETIC_UNACCEPTED'} if role=='event_curator' else output_for(role,request)
    for _ in range(2):
        with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=malformed))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=malformed))['status']=='retained'
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    result=pending.detail(settings.database,item['id'])
    assert 'SYNTHETIC_UNACCEPTED' in result['last_attempt']['output_text']
    assert len(result['messages'])==2


def test_http_pending_auth_resume_and_busy_lease(settings):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    item=retained(settings)
    client=TestClient(create_app(settings,token='synthetic',live=True))
    assert client.get('/v1/pipeline/pending/'+str(item['id'])).status_code==401
    client.headers['Authorization']='Bearer synthetic'
    assert client.get('/v1/pipeline/status').json()['pending_materials'][0]['id']==item['id']
    assert client.get('/v1/pipeline/pending/'+str(item['id'])).json()['jobs']
    assert client.post('/v1/pipeline/resume-pending',json={'pending_id':True}).status_code==400
    assert client.post('/v1/pipeline/resume-pending',json={'pending_id':item['id']}).json()['status']=='resumed'


def test_lowering_input_target_cannot_retire_an_explicitly_resumed_frame(settings):
    ingest(settings);ingest(settings,2)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text=text||?,created_at='2025-01-01T00:02:00Z'",('x'*800,))
    async def fail(role,request):
        if role=='event_writer':raise ValueError('synthetic writer failure')
        return output_for(role,request)
    for _ in range(2):
        with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))['status']=='retained'
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    save_settings(settings.database,{'pipeline':{'max_input_chars':2000}})
    pending.resume(settings.database,item['id']);calls=[]
    async def good(role,request):calls.append(role);return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=good))
    assert result['batch_id']==item['batch_id'] and result['processed_originals']==4
    assert calls==['event_writer']


def test_replanned_pending_keeps_previous_failed_output_visible(settings):
    ingest(settings)
    async def malformed(role,request):
        return {'bad_output':'SYNTHETIC_UNACCEPTED'} if role=='event_curator' else output_for(role,request)
    for _ in range(2):
        with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=malformed))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=malformed))['status']=='retained'
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    resumed=pending.resume(settings.database,item['id'],replan=True)
    assert resumed['batch_id']!=item['batch_id']
    assert 'SYNTHETIC_UNACCEPTED' in pending.detail(settings.database,item['id'])['last_attempt']['output_text']
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    p.initialize(settings.database)
    assert 'SYNTHETIC_UNACCEPTED' in pending.detail(settings.database,item['id'])['last_error']['output_text']


def test_prompt_limit_parks_even_after_an_earlier_model_failure(settings):
    ingest(settings)
    async def failed(role,request):
        if role=='event_writer':raise ValueError('synthetic failed writer')
        return output_for(role,request)
    with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=failed))
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':1}})
    async def forbidden(*args):pytest.fail('Prompt over limit reached model')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='retained'
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'


def test_mixed_batch_is_not_labeled_fully_cut_when_one_component_failed_curator(settings):
    ingest(settings);ingest(settings,2)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:02:00Z' WHERE id=3")
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:03:00Z' WHERE id=4")
    async def run(role,request):
        out=output_for(role,request)
        if role=='track_router':
            for index,row in enumerate(out['message_assignments']):row['primary_track_ref']='new:1' if index<2 else 'new:2'
            out['track_updates'].append({**out['track_updates'][0],'track_ref':'new:2','subject':'Independent synthetic plan'})
        if role=='event_curator' and min(m['id'] for m in request['component']['messages'])==3:
            raise ValueError('synthetic invalid cut')
        return out
    for _ in range(2):
        with pytest.raises(ValueError):asyncio.run(p.advance(settings.database,include_recent=True,runner=run))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=run))['status']=='retained'
    with Store(settings.database) as store:assert pending.entries(store.conn)[0]['kind']=='routing'


def large_retained(settings):
    ingest(settings)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text=text||? WHERE id=2",('PENDING_LARGE_'+'x'*45000,))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=insufficient))['pending']==2
    with Store(settings.database) as store:return pending.entries(store.conn)[0]


def test_pending_read_over_30k_uses_prompt_room_not_the_batch_input_target(settings):
    item=large_retained(settings)
    save_settings(settings.database,{'pipeline':{'max_input_chars':2000}})
    ingest(settings,2)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=adopting()))
    assert result['events']==1 and result['processed_originals']==4
    with Store(settings.database) as store:assert pending.entries(store.conn)[0]['status']=='resolved'


def test_oversized_optional_pending_is_omitted_without_blocking_new_material(settings):
    item=large_retained(settings);ingest(settings,2)
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':40000}})
    called=[]
    async def run(role,request):
        if role=='event_curator':
            called.append(request.get('context_read',False))
            if not request.get('context_read'):
                return {'context_request':{'pending_material_id':item['id'],'reason':'continue_pending'}}
            assert request['component']['pending_material_read_omitted']==item['id']
            assert 'pending_material_read' not in request['component']
            assert 'PENDING_LARGE_' not in request['prompt']
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=run))
    assert result['events']==1 and result['processed_originals']==2 and called==[False,True]
    with Store(settings.database) as store:
        assert pending.entries(store.conn)[0]['status']=='open'
        assert [row[0] for row in store.conn.execute('SELECT raw_id FROM raw_processing ORDER BY raw_id')]==[3,4]
    async def forbidden(*args):pytest.fail('Unchanged over-budget pending was called again')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'


def test_optional_pending_checks_agent_delivery_size(settings):
    item=retained(settings);ingest(settings,2)
    p.initialize(settings.database);batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json']);routed=asyncio.run(p.route_batch(settings.database,batch,data,synthetic_runner))
    data=p.save_routing_snapshot(settings.database,batch,data,routed)
    component=data['components'][0];component['pending_materials']=pending.catalog(settings.database,component)
    component=pending.read_material(settings.database,component,item['id'])
    request=p.request_for(settings.database,batch,'event_curator',component=component,context_read=True)
    assert '旧 Event 的前置待办预留' not in request['prompt']
    limit=len(request['prompt'])+len(request['rules'])
    save_settings(settings.database,{'pipeline':{'max_prompt_chars':limit}})
    assert p.context_request_fits(settings.database,batch,request,'event_curator:0:context',synthetic_runner)
    assert not p.context_request_fits(settings.database,batch,request,'event_curator:0:context',None)


# Content-level waits can end with an original-only receipt. Larger gate-only
# activities still get one evidence review before the inactivity limit.
from datetime import datetime,timedelta,timezone

from test_public_features import raw_archive

LATER=lambda days:datetime.now(timezone.utc)+timedelta(days=days)


async def gated(role,request):
    output=output_for(role,request)
    if role=='event_curator' and request['component'].get('writer_round_gate'):
        for row in output['decision_review']['events']:row['admission']={'closed_by':None}
    return output


def parked(settings,*,chars=20):
    save_settings(settings.database,{'pipeline':{'round_gate_enabled':True}})
    raw_archive(settings).ingest([
        {'source_event_id':'u1','session_id':'books','role':'user','text':'Book club plan '+'q'*chars,'created_at':'2025-01-01T00:00:00Z'},
        {'source_event_id':'a1','session_id':'books','role':'assistant','text':'Agreed to plan '+'r'*chars,'created_at':'2025-01-01T00:01:00Z'}],source='test')
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=gated))
    assert result['events']==0 and result['pending']==2
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    assert item['kind']=='writing' and item['waiting_for_continuation'] and item['plan_compatible']
    return item


def outcomes(settings):
    with Store(settings.database,read_only=True) as store:
        return [row[0] for row in store.conn.execute('SELECT outcome FROM raw_processing ORDER BY raw_id')]


def test_parked_activity_waits_through_the_continuation_window(settings):
    item=parked(settings,chars=400)
    assert pending.settle_stale(settings.database)=={'resumed':None,'blocked':None}
    assert pending.settle_stale(settings.database,clock=LATER(2))=={'resumed':None,'blocked':None}
    with Store(settings.database) as store:assert pending.entries(store.conn)[0]['status']=='open'
    assert outcomes(settings)==[] and item['originals']==2


def test_expired_tiny_exchange_keeps_originals_and_ends_without_model_retry(settings):
    item=parked(settings)
    assert pending.settle_stale(settings.database,clock=LATER(4))=={'resumed':None,'blocked':None,'archived_only':[item['id']]}
    assert outcomes(settings)==['archive_only','archive_only']
    async def forbidden(*args):pytest.fail('Archived-only material reached a model')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'
    with Store(settings.database) as store:
        closed=pending.entries(store.conn)[0]
        assert closed['status']=='archived_only'
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==2


def test_expired_substantial_activity_gets_one_ungated_pass_and_becomes_an_event(settings):
    item=parked(settings,chars=400);calls=[]
    assert pending.settle_stale(settings.database,clock=LATER(4))=={'resumed':item['id'],'blocked':None}
    # A second tick cannot start more paid work while the first is unfinished.
    assert pending.settle_stale(settings.database,clock=LATER(4))=={'resumed':None,'blocked':None}
    with Store(settings.database) as store:
        frozen=json.loads(store.conn.execute("SELECT input_json FROM pipeline_batches WHERE status='pending'").fetchone()[0])
    assert frozen['round_gate_expired'] is True and frozen['input_policy']['round_gate_enabled'] is False
    assert frozen['resume_pending_id']==item['id'] and [m['id'] for m in frozen['messages']]==item['source_ids']
    async def run(role,request):
        calls.append(role)
        if role=='event_curator':assert not request['component'].get('writer_round_gate')
        return await gated(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=run))
    assert result['status']=='processed' and result['events']==1 and calls==['event_curator','event_writer']
    assert outcomes(settings)==['settled','settled']
    with Store(settings.database) as store:
        done=pending.entries(store.conn)[0]
        assert done['status']=='resolved' and len(done['resolved_event_ids'])==1
    # The gate setting itself is untouched for ordinary new material.
    assert p.read_settings(settings.database)['pipeline']['round_gate_enabled'] is True


def test_expired_activity_archives_insufficient_evidence_without_repeating_paid_work(settings):
    item=parked(settings,chars=400)
    assert pending.settle_stale(settings.database,clock=LATER(4))['resumed']==item['id']
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=insufficient))['pending']==2
    with Store(settings.database) as store:
        again=pending.entries(store.conn)[0]
        assert again['status']=='open' and not again['waiting_for_continuation']
    assert pending.settle_stale(settings.database,clock=LATER(9))=={'resumed':None,'blocked':None,'archived_only':[item['id']]}
    async def forbidden(*args):pytest.fail('Expired material was sent to a model twice')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'
    assert outcomes(settings)==['archive_only','archive_only']
    with Store(settings.database) as store:
        kept=pending.entries(store.conn)[0]
        assert kept['status']=='archived_only' and kept['originals']==2
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==2


def test_expired_pass_cannot_place_a_repair_hold_on_the_chat(settings,monkeypatch):
    from serein.extensions import pipeline_recovery as recovery
    item=parked(settings,chars=400)
    assert pending.settle_stale(settings.database,clock=LATER(4))['resumed']==item['id']
    def broken(*args):raise p.RoutingRecoveryError('synthetic frozen plan mismatch')
    monkeypatch.setattr(recovery,'assert_downstream_snapshot',broken)
    async def forbidden(*args):pytest.fail('A rejected plan reached a model')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='retained'
    assert p.scope_holds(settings.database)==[]
    with Store(settings.database) as store:
        assert not store.conn.execute("SELECT 1 FROM pipeline_batches WHERE status='needs_repair'").fetchone()
        kept=pending.entries(store.conn)[0]
        assert kept['status']=='open' and kept['kind']!='unrouted' and kept['batch_status']=='waiting_materials'
    monkeypatch.undo()
    # It stays visible for a person; expiry does not retry it automatically.
    assert pending.settle_stale(settings.database,clock=LATER(9))=={'resumed':None,'blocked':None}


def test_unverifiable_old_plan_is_left_parked_instead_of_resumed(settings):
    item=parked(settings,chars=200)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_batches SET input_json=json_remove(input_json,'$.routing_result') WHERE id=?",(item['batch_id'],))
    assert pending.settle_stale(settings.database,clock=LATER(4))=={'resumed':None,'blocked':item['id']}
    with Store(settings.database) as store:
        assert not pending.entries(store.conn)[0]['waiting_for_continuation']
        assert '自动重审未启动' in pending.entries(store.conn)[0]['reason']
        assert not store.conn.execute("SELECT 1 FROM pipeline_batches WHERE status IN ('pending','needs_repair')").fetchone()
    assert pending.settle_stale(settings.database,clock=LATER(9))=={'resumed':None,'blocked':None}
    assert outcomes(settings)==[]


@pytest.mark.parametrize('key',pending._HOST_DEFERRALS)
def test_host_deferrals_are_never_hidden_as_normal_waiting(settings,key):
    item=parked(settings)
    with Store(settings.database) as store:
        row=store.conn.execute('SELECT result_json FROM pipeline_batches WHERE id=?',(item['batch_id'],)).fetchone()
        result=json.loads(row[0]);result[key]=[{'source_message_ids':item['source_ids']}]
        store.conn.execute('UPDATE pipeline_batches SET result_json=? WHERE id=?',(json.dumps(result),item['batch_id']))
        assert pending.entries(store.conn)[0]['waiting_for_continuation'] is False
    assert pending.settle_stale(settings.database,clock=LATER(4))=={'resumed':None,'blocked':None}
    assert outcomes(settings)==[]


def test_unverified_curator_output_is_not_hidden_or_retried(settings):
    item=parked(settings)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_jobs SET request_json='{}' WHERE batch_id=? AND role='event_curator:0'",(item['batch_id'],))
        assert not pending.entries(store.conn)[0]['waiting_for_continuation']
    assert pending.settle_stale(settings.database,clock=LATER(4))=={'resumed':None,'blocked':None}
    assert outcomes(settings)==[]


def test_accepted_writer_insufficiency_with_gate_enabled_can_end_as_originals(settings):
    save_settings(settings.database,{'pipeline':{'round_gate_enabled':True}})
    for index,role in enumerate(('user','assistant','user','assistant')):
        raw_archive(settings).ingest([{'source_event_id':str(index),'session_id':'books','role':role,
            'text':'A real planning exchange '+str(index),'created_at':f'2025-01-01T00:0{index}:00Z'}],source='test')
    async def insufficient_writer(role,request):
        if role=='event_writer':return {'evidence_sufficient':False,'self_review':{}}
        return await gated(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=insufficient_writer))
    assert result['pending']==4
    with Store(settings.database) as store:
        item=pending.entries(store.conn)[0]
        assert item['batch_status']=='done' and not item['waiting_for_continuation']
    assert pending.settle_stale(settings.database,clock=LATER(4))=={'resumed':None,'blocked':None,'archived_only':[item['id']]}
    assert outcomes(settings)==['archive_only']*4


def test_failed_automatic_preflight_is_durable_and_remains_visible(settings,monkeypatch):
    item=parked(settings,chars=400);calls=[]
    def refused(*args,**kwargs):
        calls.append(1)
        raise ValueError('synthetic preflight conflict')
    monkeypatch.setattr(pending,'resume',refused)
    assert pending.settle_stale(settings.database,clock=LATER(4))=={'resumed':None,'blocked':item['id']}
    assert pending.settle_stale(settings.database,clock=LATER(9))=={'resumed':None,'blocked':None}
    assert calls==[1] and outcomes(settings)==[]
    with Store(settings.database) as store:
        kept=pending.entries(store.conn)[0]
        assert kept['status']=='open' and not kept['waiting_for_continuation']
        assert 'synthetic preflight conflict' in kept['reason']


def test_expiry_protects_recently_ingested_old_history(settings):
    item=parked(settings)
    assert pending.settle_stale(settings.database)=={'resumed':None,'blocked':None}
    with Store(settings.database) as store:
        assert pending.entries(store.conn)[0]['status']=='open'
    assert item['locator']['first_at'].startswith('2025-01-01') and outcomes(settings)==[]


def test_refused_resume_reports_the_reason_without_failing_the_pipeline(settings):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    item=parked(settings,chars=200)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_batches SET input_json=json_set(input_json,'$.runtime_revision','older-build') WHERE id=?",(item['batch_id'],))
        assert pending.entries(store.conn)[0]['plan_compatible'] is False
    client=TestClient(create_app(settings,token='synthetic',live=True));client.headers['Authorization']='Bearer synthetic'
    before=client.get('/v1/pipeline/status').json()
    refused=client.post('/v1/pipeline/resume-pending',json={'pending_id':item['id']})
    assert refused.status_code>=400 and '版本已变化' in refused.text
    after=client.get('/v1/pipeline/status').json()
    assert after['status']==before['status']!='failed' and not after.get('error')
    assert client.post('/v1/pipeline/resume-pending',json={'pending_id':item['id'],'replan':True}).json()['status']=='resumed'


def test_scheduled_pass_reviews_parked_work_and_survives_its_failure(settings,monkeypatch):
    from test_route_scope_holds import api,Clock
    api(settings,monkeypatch,lambda request:output_for(request['role'],request));seen=[]
    monkeypatch.setattr(pending,'settle_stale',lambda database,clock=None:seen.append(clock))
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='current'
    assert seen==[Clock.value]
    def broken(database,clock=None):raise RuntimeError('synthetic review failure')
    monkeypatch.setattr(pending,'settle_stale',broken)
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='settled_today'
