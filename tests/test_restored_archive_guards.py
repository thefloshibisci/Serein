"""Deleting an original revokes its pending preview and in-flight Writer task."""
import asyncio

import pytest

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Conflict, Store, encode
from serein.extensions import pipeline as p, pipeline_pending as pending
from serein.pipeline_source_guard import check
from serein.source_management import SourceManagement
from test_public_features import settings, ingest, output_for


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
        assert pending.entries(st.conn) == []
        assert st.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==0
        assert st.conn.execute('SELECT status FROM pipeline_pending_materials').fetchone()[0]=='cancelled_source_deleted'
        assert st.conn.execute('SELECT status FROM pipeline_batches').fetchone()[0]=='superseded_source_deleted'
    with pytest.raises(Conflict):pending.detail(s.database,1)
    with pytest.raises(Conflict):check(s.database,data)
    with Store(s.database, read_only=True) as store:
        assert not store.conn.execute("SELECT 1 FROM pipeline_pending_materials WHERE status IN ('open','running')").fetchone()


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


def test_deleted_context_blocks_request_even_if_owned_survives(tmp_path):
    s,manager,preview,data=seed_deleted_case(tmp_path)
    frozen={'component':{'context_messages':data['messages']},'messages':[]}
    manager.delete(preview['items'],True)
    with pytest.raises(Conflict):check(s.database,frozen)


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
