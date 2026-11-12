"""Terminal original-only receipts preserve evidence without recurring Writer calls."""
import asyncio
import json
from datetime import datetime,timedelta,timezone

import pytest

from test_public_features import settings,ingest,output_for,synthetic_runner,raw_archive
from test_pipeline_pending import retained,parked,insufficient
from serein.core.store import Store,Conflict,encode
from serein.extensions import pipeline as p
from serein.extensions import pipeline_pending as pending


def later(days):return datetime.now(timezone.utc)+timedelta(days=days)


def snapshot(settings):
    with Store(settings.database,read_only=True) as store:
        return {table:[dict(row) for row in store.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('raw_events','fact_events','pipeline_jobs')}


def state(settings,pending_id):
    with Store(settings.database,read_only=True) as store:
        return next(row for row in pending.entries(store.conn) if row['id']==pending_id)


def test_manual_archive_done_remainder_is_atomic_idempotent_and_keeps_saved_events(settings):
    ingest(settings)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    ingest(settings,2)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=insufficient))['pending']==2
    with Store(settings.database) as store:
        item=next(row for row in pending.entries(store.conn) if row['status']=='open')
        old_result=store.conn.execute('SELECT result_json FROM pipeline_batches WHERE id=?',(item['batch_id'],)).fetchone()[0]
        # A version mismatch blocks old execution, not an exact original-only exit.
        store.conn.execute("UPDATE pipeline_batches SET input_json=json_set(input_json,'$.runtime_revision','old-build') WHERE id=?",(item['batch_id'],))
    before=snapshot(settings)
    result=pending.archive(settings.database,item['id'],pending.ARCHIVE_CONFIRM)
    assert result['status']=='archived_only' and result['originals']==2
    assert pending.archive(settings.database,item['id'],pending.ARCHIVE_CONFIRM)==result
    assert snapshot(settings)==before
    assert state(settings,item['id'])['status']=='archived_only'
    assert pending.detail(settings.database,item['id'])['closure_receipt']==result['closure_receipt']
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT result_json FROM pipeline_batches WHERE id=?',(item['batch_id'],)).fetchone()[0]==old_result
        assert [r[0] for r in store.conn.execute('SELECT outcome FROM raw_processing ORDER BY raw_id')]==['settled','settled','archive_only','archive_only']
    with pytest.raises(Conflict):pending.resume(settings.database,item['id'],replan=True)


@pytest.mark.parametrize('automatic',[False,True])
def test_finishing_remainder_keeps_event_and_result_from_the_same_batch(settings,automatic):
    ingest(settings);ingest(settings,2)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:02:00Z' WHERE id=3")
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:03:00Z' WHERE id=4")
    async def partial(role,request):
        out=output_for(role,request)
        if role=='track_router':
            for index,assignment in enumerate(out['message_assignments']):
                assignment['primary_track_ref']='new:1' if index<2 else 'new:2'
            out['track_updates'].append({**out['track_updates'][0],'track_ref':'new:2','subject':'Another topic'})
        if role=='event_writer' and min(m['id'] for m in request['messages'])==1:
            return {'evidence_sufficient':False,'self_review':{}}
        return out
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=partial))
    assert result['events']==1 and result['pending']==2
    with Store(settings.database) as store:
        item=pending.entries(store.conn)[0]
        batch_before=dict(store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(item['batch_id'],)).fetchone())
    before=snapshot(settings)
    if automatic:assert pending.archive_stale(settings.database,clock=later(2))==[item['id']]
    else:pending.archive(settings.database,item['id'],pending.ARCHIVE_CONFIRM)
    assert snapshot(settings)==before
    with Store(settings.database) as store:
        assert dict(store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(item['batch_id'],)).fetchone())==batch_before
        assert [r[0] for r in store.conn.execute('SELECT outcome FROM raw_processing ORDER BY raw_id')]==['archive_only','archive_only','settled','settled']


@pytest.mark.parametrize('partial',[False,True])
def test_manual_unfinished_batch_requires_complete_remaining_ownership(settings,partial):
    item=retained(settings)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE pipeline_batches SET status='waiting_materials',result_json=? WHERE id=?",
                           (encode({'error':'Synthetic original failure'}),item['batch_id']))
        if partial:store.conn.execute('UPDATE pipeline_pending_materials SET source_ids_json=? WHERE id=?',
                                      (encode(item['source_ids'][:1]),item['id']))
    before=snapshot(settings)
    if partial:
        with pytest.raises(Conflict,match='还有不属于'):pending.archive(settings.database,item['id'],pending.ARCHIVE_CONFIRM)
        assert state(settings,item['id'])['status']=='open'
    else:
        pending.archive(settings.database,item['id'],pending.ARCHIVE_CONFIRM)
        with Store(settings.database) as store:
            batch=store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(item['batch_id'],)).fetchone()
            assert batch['status']=='done' and json.loads(batch['result_json'])['previous_result']=={'error':'Synthetic original failure'}
    assert snapshot(settings)==before


def test_archive_transaction_rolls_back_if_receipt_write_fails(settings):
    import sqlite3
    item=retained(settings)
    with Store(settings.database) as store:
        store.conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON pipeline_pending_archive_receipts BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='synthetic receipt failure'):
        pending.archive(settings.database,item['id'],pending.ARCHIVE_CONFIRM)
    assert state(settings,item['id'])['status']=='open'
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM pipeline_pending_archive_receipts').fetchone()[0]==0


@pytest.mark.parametrize('retained_days',[-90,90])
def test_acquisition_time_protects_old_imports_and_retry_time_cannot_renew_them(settings,retained_days):
    item=retained(settings)
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT min(created_at) FROM raw_events').fetchone()[0].startswith('2025-01-01')
        store.conn.execute('UPDATE pipeline_pending_materials SET updated_at=? WHERE id=?',
                           (later(retained_days).isoformat(),item['id']))
    assert pending.archive_stale(settings.database,clock=later(.5))==[]
    assert pending.archive_stale(settings.database,clock=later(2))==[item['id']]


@pytest.mark.parametrize('reason',['raw_changed','already_processed','shared_pending','active_owner','adoption','accepted_curator'])
def test_archive_refuses_changed_or_shared_ownership(settings,reason):
    item=retained(settings)
    with Store(settings.database) as store:
        if reason=='raw_changed':store.conn.execute("UPDATE raw_events SET text='changed' WHERE id=?",(item['source_ids'][0],))
        elif reason=='already_processed':store.conn.execute("INSERT INTO raw_processing VALUES (?,'other','settled')",(item['source_ids'][0],))
        else:
            data={'messages':[],'routing_messages':[],'components':[]}
            if reason=='active_owner':data['messages']=[{'id':item['source_ids'][0]}]
            if reason=='adoption':data['components']=[{'pending_material_read':{'id':item['id']}}]
            store.conn.execute("INSERT INTO pipeline_batches VALUES ('other','other',?,'waiting_materials','{}')",(encode(data),))
            if reason=='shared_pending':store.conn.execute("INSERT INTO pipeline_pending_materials(batch_id,scope,status,kind,source_ids_json,reason,updated_at) VALUES ('other','other','open','writing',?,'held','2025-01-01')",(encode(item['source_ids']),))
            if reason=='accepted_curator':store.conn.execute("INSERT INTO pipeline_jobs VALUES ('other:c','other','event_curator:0','{}',?)",(encode({'adopt_pending_material_ids':[item['id']]}),))
    before=snapshot(settings)
    with pytest.raises(Conflict):pending.archive(settings.database,item['id'],pending.ARCHIVE_CONFIRM)
    assert snapshot(settings)==before and state(settings,item['id'])['status']=='open'


@pytest.mark.parametrize('kind',['writer','gate'])
def test_short_proven_deferral_closes_without_more_model_work(settings,kind):
    item=retained(settings) if kind=='writer' else parked(settings)
    before=snapshot(settings)
    assert pending.archive_stale(settings.database,clock=later(.5))==[]
    assert pending.archive_stale(settings.database,clock=later(2))==[item['id']]
    assert pending.archive_stale(settings.database,clock=later(9))==[]
    assert snapshot(settings)==before
    receipt=state(settings,item['id'])['closure_receipt']
    assert receipt['proof']['reason']=='short_fragment_inactive' and receipt['automatic']
    assert receipt['proof']['accepted_jobs']
    async def forbidden(*args):pytest.fail('Archived originals reached a model again')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'


def test_accepted_curator_deferral_closes_after_topic_inactivity(settings):
    ingest(settings)
    with Store(settings.database) as store:store.conn.execute("UPDATE raw_events SET text=text||?",('x'*500,))
    raw_archive(settings).ingest([{'source_event_id':'tail','session_id':'books','role':'user',
        'text':'Wait, the plan needs clarification','created_at':'2025-01-01T18:55:00Z'}],source='test')
    p.initialize(settings.database)
    p.new_batch(settings.database,False,datetime.fromisoformat('2025-01-02T04:00:00+08:00'))
    async def defer(role,request):
        result=output_for(role,request)
        if role=='event_curator':
            roots=result['events'][0]['owned_unit_roots']
            result.update(events=[],defer_unit_roots=roots,
                          decision_review={'events':[],'boundaries':[],
                            'dispositions':[{'disposition':'defer','unit_roots':roots,
                                             'parked_source_message_ids':request['component']['parked_context_source_ids'],
                                             'reason':'Wait for clarification'}]})
        return result
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=defer))['events']==0
    with Store(settings.database) as store:item=pending.entries(store.conn)[0]
    assert pending.archive_stale(settings.database,clock=later(2))==[]
    assert item['originals']==2 and pending.archive_stale(settings.database,clock=later(8))==[item['id']]
    assert state(settings,item['id'])['closure_receipt']['proof']['content_reasons']==['curator_deferred']


@pytest.mark.parametrize('fault',['network','missing_image','invalid_output'])
def test_errors_and_unverified_evidence_are_not_auto_archived(settings,fault):
    item=retained(settings)
    with Store(settings.database) as store:
        if fault=='network':
            store.conn.execute("UPDATE pipeline_jobs SET output_json=NULL WHERE role LIKE 'event_writer:%'")
            store.conn.execute("UPDATE pipeline_batches SET status='waiting_materials' WHERE id=?",(item['batch_id'],))
        elif fault=='missing_image':store.conn.execute("UPDATE pipeline_batches SET result_json=json_set(result_json,'$.missing_images',json('[1]')) WHERE id=?",(item['batch_id'],))
        else:store.conn.execute("UPDATE pipeline_jobs SET output_json=? WHERE role LIKE 'event_writer:%'",(encode({'evidence_sufficient':False,'event_draft':'unvalidated'}),))
    assert pending.archive_stale(settings.database,clock=later(20))==[]
    assert state(settings,item['id'])['status']=='open'


@pytest.mark.parametrize('route',['same','other','unknown','missing_provenance','changed_provenance'])
def test_followup_is_track_scoped_not_whole_conversation(settings,route):
    item=retained(settings)
    with Store(settings.database) as store:
        data=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(item['batch_id'],)).fetchone()[0])
        track=data['routing_result']['assignments'][0]['primary_track_id']
    result=raw_archive(settings).ingest([
        {'source_event_id':'new','session_id':'books','role':'user','text':'A new independent topic','created_at':'2025-01-02T00:00:00Z'},
        {'source_event_id':'reply','session_id':'books','role':'assistant','text':'An independent reply','created_at':'2025-01-02T00:01:00Z'}],source='test')
    key=result['items'][0]['id']
    with Store(settings.database) as store:
        for raw in result['items']:store.conn.execute('UPDATE raw_events SET ingested_at=? WHERE id=?',(later(1.5).isoformat(),raw['id']))
    if route!='unknown':
        async def route_followup(role,request):
            out=output_for(role,request)
            if role=='track_router' and route!='same':
                for assignment in out['message_assignments']:assignment['primary_track_ref']='new:1'
                out['track_updates']=[{**out['track_updates'][0],'track_ref':'new:1','subject':'Independent topic'}]
            return out
        batch=p.new_batch(settings.database,True,later(2))
        data=json.loads(batch['input_json'])
        asyncio.run(p._publish_routes(settings.database,batch,data,route_followup))
        with Store(settings.database) as store:
            following=json.loads(store.conn.execute('SELECT route_json FROM pipeline_routes WHERE raw_id=?',(key,)).fetchone()[0])
            assert (following['primary_track_id']==track)==(route=='same')
            if route=='missing_provenance':store.conn.execute('DELETE FROM pipeline_route_provenance WHERE raw_id=?',(key,))
            if route=='changed_provenance':store.conn.execute("UPDATE pipeline_route_provenance SET route_json='{}' WHERE raw_id=?",(key,))
    assert pending.archive_stale(settings.database,clock=later(2))==([item['id']] if route=='other' else [])


def test_cross_session_same_track_delays_auto_archive(settings):
    item=retained(settings);clock=datetime.now(timezone.utc)
    incoming=raw_archive(settings).ingest([
        {'source_event_id':'next-u','session_id':'another-window','role':'user',
         'text':'Book club continuation','created_at':'2025-01-02T00:00:00Z'},
        {'source_event_id':'next-a','session_id':'another-window','role':'assistant',
         'text':'Agreed in another window','created_at':'2025-01-02T00:01:00Z'}],source='test')
    ids=[row['id'] for row in incoming['items']]
    with Store(settings.database) as store:
        for raw_id in ids:store.conn.execute('UPDATE raw_events SET ingested_at=? WHERE id=?',
                                             ((clock+timedelta(days=1.5)).isoformat(),raw_id))
    batch=p.new_batch(settings.database,True,clock+timedelta(days=2))
    asyncio.run(p._publish_routes(settings.database,batch,json.loads(batch['input_json']),synthetic_runner))
    with Store(settings.database) as store:
        original=json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(item['batch_id'],)).fetchone()[0])
        later_track=json.loads(store.conn.execute('SELECT route_json FROM pipeline_routes WHERE raw_id=?',(ids[0],)).fetchone()[0])['primary_track_id']
        assert original['routing_result']['assignments'][0]['primary_track_id']==later_track
        assert store.conn.execute('SELECT count(DISTINCT session_id) FROM raw_events').fetchone()[0]==2
        assert store.conn.execute('SELECT count(*) FROM pipeline_route_provenance WHERE raw_id IN (?,?)',ids).fetchone()[0]==2
    assert pending.archive_stale(settings.database,clock=clock+timedelta(days=2))==[]
    assert state(settings,item['id'])['status']=='open'


def test_archive_http_requires_confirmation_and_repeated_calls_return_same_receipt(settings):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    item=retained(settings)
    client=TestClient(create_app(settings,token='synthetic',live=True))
    payload={'pending_id':item['id'],'confirm':pending.ARCHIVE_CONFIRM}
    assert client.post('/v1/pipeline/archive-pending',json=payload).status_code==401
    client.headers['Authorization']='Bearer synthetic'
    assert client.post('/v1/pipeline/archive-pending',json={'pending_id':item['id']}).status_code==400
    first=client.post('/v1/pipeline/archive-pending',json=payload)
    assert first.status_code==200 and first.json()['status']=='archived_only'
    assert client.post('/v1/pipeline/archive-pending',json=payload).json()==first.json()
    current=client.get('/v1/pipeline/status').json()
    assert next(row for row in current['pending_materials'] if row['id']==item['id'])['status']=='archived_only'
