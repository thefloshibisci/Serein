"""Synthetic regressions for automatic recovery and revoked originals."""
import asyncio
from datetime import datetime
from unittest.mock import patch
import pytest
from serein.config import Settings
from serein.bootstrap import initialize
from serein.core.store import Store,encode,Conflict
from serein.source_management import SourceManagement
from serein.extensions import pipeline as p,pipeline_pending as pending
from serein.pipeline_source_guard import check
from serein.pipeline_automatic import pending_status
from test_public_features import settings,ingest,output_for,synthetic_runner


def seed_deleted_case(tmp_path):
    s=Settings(tmp_path/'delete.db',writable=True);initialize(s);p.initialize(s.database)
    manager=SourceManagement(s)
    result=manager.upload('synthetic','case',[{'message_id':'m1','role':'user','content':'SYNTHETIC_REMOVED_DETAIL','created_at':'2026-11-13T10:00:00+08:00'}])
    rid=int(result['items'][0]['id'].split(':')[1])
    with Store(s.database) as st:
        m=p.task_message(st.conn.execute('SELECT * FROM raw_events WHERE id=?',(rid,)).fetchone())
        data={'messages':[m],'routing_messages':[m],'components':[],'scope':'synthetic','contract':p.CONTRACT,'runtime_revision':p.runtime_revision()}
        st.conn.execute("INSERT INTO pipeline_batches VALUES ('fake','synthetic',?,'done','{}')",(encode(data),))
        st.conn.execute("INSERT INTO pipeline_pending_materials(batch_id,scope,status,kind,source_ids_json,reason,updated_at) VALUES ('fake','synthetic','open','writing',?,'synthetic','2026-11-13')",(encode([rid]),))
    preview=manager.preview_delete(['raw:'+str(rid)])
    return s,manager,preview,data


def test_delete_revokes_pending_snapshot_and_inflight_input(tmp_path):
    s,manager,preview,data=seed_deleted_case(tmp_path)
    assert preview['can_delete']
    manager.delete(preview['items'],True)
    with Store(s.database,read_only=True) as st:
        assert st.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==0
        assert st.conn.execute('SELECT status FROM pipeline_pending_materials').fetchone()[0]=='cancelled_source_deleted'
        assert st.conn.execute('SELECT status FROM pipeline_batches').fetchone()[0]=='superseded_source_deleted'
    with pytest.raises(Conflict):pending.detail(s.database,1)
    with pytest.raises(Conflict):check(s.database,data)
    assert pending_status(s.database) is None


def test_running_writer_deleted_mid_request_cannot_publish(settings):
    ingest(settings)
    async def runner(role,request):
        out=output_for(role,request)
        if role=='event_writer':
            from serein.source_management import initialize as init_tombs
            from serein.core.store import digest,now
            with Store(settings.database) as st,st.transaction(immediate=True):
                init_tombs(st.conn)
                r=st.conn.execute('SELECT * FROM raw_events WHERE id=1').fetchone()
                st.conn.execute('INSERT INTO raw_message_tombstones VALUES (?,?,?,?,?,?)',(1,r['source'],r['source_event_id'],r['event_hash'],now(),digest(r['text'])))
                st.conn.execute('DELETE FROM raw_events WHERE id=1')
        return out
    try:asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    except (ValueError,Conflict):pass
    with Store(settings.database,read_only=True) as st:
        assert st.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==0
        assert st.conn.execute("SELECT count(*) FROM pipeline_jobs WHERE role LIKE 'event_writer%' AND output_json IS NOT NULL").fetchone()[0]==0


def test_gate_only_wait_is_resumed_by_timer(settings):
    from serein.deployment import save_settings
    save_settings(settings.database,{'pipeline':{'round_gate_enabled':True}})
    ingest(settings)
    async def gated(role,request):
        out=output_for(role,request)
        if role=='event_curator':out['decision_review']['events'][0]['admission']={'closed_by':None}
        return out
    first=asyncio.run(p.advance(settings.database,include_recent=True,runner=gated))
    assert first['events']==0 and first['pending']==2
    from serein.pipeline_automatic import resume_gate_wait
    assert resume_gate_wait(settings.database)['status']=='resumed'
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    assert result['events']==1
    assert pending_status(settings.database) is None


def test_timer_does_not_claim_completed_with_pending(tmp_path):
    s,_,_,_=seed_deleted_case(tmp_path)
    class FixedDate(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,11,15,4,0,tzinfo=p.TZ)
    async def empty(*a,**k):return {'status':'current'}
    async def flush(*a,**k):pass
    with patch.object(p,'datetime',FixedDate),patch.object(p,'_advance',empty),patch.object(p,'flush_routes',flush),patch.object(p,'held_result',return_value=None),patch('serein.pipeline_automatic.resume_gate_wait',return_value=None):
        result=asyncio.run(p.scheduled_advance(s.database))
    assert result['status']=='pending_materials' and result['pending_count']==1
    with Store(s.database,read_only=True) as st:
        assert st.conn.execute("SELECT completed FROM pipeline_schedule WHERE day='2026-11-15'").fetchone()[0]==0
        import json
        observation=json.loads(st.conn.execute("SELECT value_json FROM background_state WHERE name='pipeline_auto_observation'").fetchone()[0])
        assert observation['phase']=='pending_materials'
        assert observation['checked_at'] and observation['finished_at']


def test_deleted_context_blocks_request_even_if_owned_survives(tmp_path):
    s,manager,preview,data=seed_deleted_case(tmp_path)
    frozen={'component':{'context_messages':data['messages']},'messages':[]}
    manager.delete(preview['items'],True)
    with pytest.raises(Conflict):check(s.database,frozen)


@pytest.mark.parametrize("reason", ["Upstream returned HTTP 503", "Upstream returned HTTP 513", "Upstream returned HTTP 529"])
def test_transient_recovery_has_durable_limit(tmp_path,reason):
    s,_,_,_=seed_deleted_case(tmp_path)
    from serein.pipeline_automatic import resume_transient_failure
    error={'error':reason,'created_at':'2026-11-01T00:00:00+00:00'}
    with patch.object(pending,'last_attempt',return_value=error),patch.object(pending,'resume',return_value={'status':'resumed'}) as resume:
        assert resume_transient_failure(s.database)['status']=='resumed'
        assert resume.call_count==1
        assert resume_transient_failure(s.database) is None
        with Store(s.database) as st:
            st.conn.execute("UPDATE background_state SET value_json=? WHERE name='pipeline_auto_retry:1'",(encode({'attempts':3,'attempted_at':'2026-11-01T00:00:00+00:00'}),))
        assert resume_transient_failure(s.database) is None
        assert resume.call_count==1


def test_exact_evidence_reconciles_ledger_without_new_event(settings):
    from serein.pipeline_automatic import reconcile_covered_pending
    ingest(settings)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    with Store(settings.database) as st:
        batch=st.conn.execute('SELECT id,scope FROM pipeline_batches').fetchone()
        st.conn.execute('DELETE FROM raw_processing WHERE raw_id IN (1,2)')
        st.conn.execute("INSERT INTO pipeline_pending_materials(batch_id,scope,status,kind,source_ids_json,reason,updated_at) VALUES (?,?,'open','writing','[1,2]','synthetic','2026-11-15')",(batch['id'],batch['scope']))
        before=st.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]
    assert reconcile_covered_pending(settings.database)
    with Store(settings.database) as st:
        assert st.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0]==before
        assert st.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==2
        assert st.conn.execute('SELECT status FROM pipeline_pending_materials').fetchone()[0]=='resolved'


def test_bound_source_ids_are_not_raw_message_ids(settings):
    from serein.pipeline_source_guard import message_ids
    ingest(settings)
    with Store(settings.database) as st:
        original=p.task_message(st.conn.execute('SELECT * FROM raw_events WHERE id=1').fetchone())
    bound={'id':9999,'role':'user','source_system':'test','message_id':'u1',
           'content':'Book club plan 1','session_id':'books'}
    frozen={'messages':[original],'base_event_candidates':[{'source_refs':[bound]}]}
    assert message_ids(frozen)=={1}
    check(settings.database,frozen)
    # Even an existing raw ID must not be confused with a bound-source row.
    bound['id']=2
    assert message_ids({'source_refs':[bound]})==set()


@pytest.mark.parametrize("reason", ["Upstream returned HTTP 400", "Upstream returned HTTP 401", "Upstream returned HTTP 403", "Upstream returned HTTP 501", "Upstream returned HTTP 505", "HTTP 5130", "证据不足", "用户暂停"])
def test_automatic_recovery_leaves_permanent_errors_alone(tmp_path,reason):
    s,_,_,_=seed_deleted_case(tmp_path)
    from serein.pipeline_automatic import resume_transient_failure
    error={'error':reason,'created_at':'2026-11-01T00:00:00+00:00'}
    with patch.object(pending,'last_attempt',return_value=error),patch.object(pending,'resume') as resume:
        assert resume_transient_failure(s.database) is None
        resume.assert_not_called()
