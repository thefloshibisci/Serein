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


def test_finishing_remainder_keeps_event_and_result_from_the_same_batch(settings):
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
    pending.archive(settings.database,item['id'],pending.ARCHIVE_CONFIRM)
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
