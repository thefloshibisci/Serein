"""Continuous service scenarios using isolated originals and synthetic models."""
import asyncio
import json
from datetime import timedelta

import pytest

from test_public_features import settings, output_for, ingest, synthetic_runner
from test_route_scope_holds import Clock, seed, api, reset_clock
from serein.core.store import Store, encode
from serein.extensions import pipeline as p


def test_ready_event_settles_before_slow_daytime_router(settings, monkeypatch):
    seed(settings,session='ready',start=Clock.value.replace(hour=1,minute=0),count=1)
    seed(settings,session='daytime',start=Clock.value.replace(hour=3,minute=0),count=5)
    calls=api(settings,monkeypatch,lambda request:output_for(request['role'],request))
    first=asyncio.run(p.scheduled_advance(settings.database))
    assert first['status']=='processed' and first['processed_originals']==2
    assert all(m['original_session_id']=='ready' for request in calls for m in request.get('messages',[]))
    # Only the next lease processes daytime material; it never passes 03:00.
    Clock.value=Clock.value.replace(hour=4,minute=0)
    second=asyncio.run(p.scheduled_advance(settings.database))
    assert second['status'] in ('current','settled_today')
    with Store(settings.database,read_only=True) as store:
        assert [r[0] for r in store.conn.execute('SELECT raw_id FROM raw_processing ORDER BY raw_id')]==[1,2]
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes WHERE raw_id>2').fetchone()[0]==10


def test_daytime_pass_yields_after_one_complete_transport_frame(settings, monkeypatch):
    Clock.value=Clock.value.replace(hour=2,minute=30)
    seed(settings,session='long',start=Clock.value-timedelta(hours=5),count=25)
    calls=api(settings,monkeypatch,lambda request:output_for(request['role'],request))
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='waiting_settlement_window'
    assert len(calls)==1 and len(calls[0]['messages'])==40
    assert [row['role'] for row in calls[0]['messages']]==['user','assistant']*20
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='waiting_settlement_window'
    assert len(calls)==2 and len(calls[1]['messages'])==10
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0]==50
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='waiting_settlement_window'
    assert len(calls)==2  # Fully cached input makes no additional paid calls.


def test_daytime_pass_releases_lease_between_independent_scopes(settings, monkeypatch):
    Clock.value=Clock.value.replace(hour=2,minute=30)
    for session in ('one','two'):
        seed(settings,session=session,start=Clock.value-timedelta(hours=2),count=5)
    calls=api(settings,monkeypatch,lambda request:output_for(request['role'],request))
    asyncio.run(p.scheduled_advance(settings.database))
    assert len(calls)==1
    asyncio.run(p.scheduled_advance(settings.database))
    assert len(calls)==2
    assert [request['messages'][0]['original_session_id'] for request in calls]==['one','two']


def test_continuous_arrivals_do_not_starve_older_uncached_scope(settings,monkeypatch):
    Clock.value=Clock.value.replace(hour=2,minute=30)
    for session in ('one','two'):
        seed(settings,session=session,start=Clock.value-timedelta(hours=2),count=5)
    calls=api(settings,monkeypatch,lambda request:output_for(request['role'],request))
    asyncio.run(p.scheduled_advance(settings.database))
    for index in range(3):
        seed(settings,session='one',start=Clock.value-timedelta(hours=1)+timedelta(minutes=index*5),count=1)
        asyncio.run(p.scheduled_advance(settings.database))
    assert [request['messages'][0]['original_session_id'] for request in calls]==['one','two','one','one']
    assert [[message['id'] for message in request['messages']] for request in calls]==[
        list(range(1,11)),list(range(11,21)),list(range(21,25)),[25,26]]
    assert all([message['role'] for message in request['messages']]==
               ['user','assistant']*(len(request['messages'])//2) for request in calls)
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0]==26
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==26
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_late_reply_cached_prefix_does_not_jump_older_uncached_scope(settings,monkeypatch):
    from serein.compat.raw_archive import raw_archive
    Clock.value=Clock.value.replace(hour=2,minute=30)
    for session in ('one','two'):
        seed(settings,session=session,start=Clock.value-timedelta(hours=2),count=5)
    calls=api(settings,monkeypatch,lambda request:output_for(request['role'],request))
    asyncio.run(p.scheduled_advance(settings.database))
    for index in range(3):
        raw_archive(settings).ingest([{'source_event_id':f'late-{index}','session_id':'one',
            'role':'assistant','text':f'Later book club answer {index}',
            'created_at':(Clock.value-timedelta(hours=1)+timedelta(minutes=index)).isoformat()}],source='test')
        asyncio.run(p.scheduled_advance(settings.database))
    assert [request['messages'][0]['original_session_id'] for request in calls]==['one','two','one','one']
    assert [[message['id'] for message in request['messages']] for request in calls]==[
        list(range(1,11)),list(range(11,21)),[9,10,21,22],[9,10,21,22,23]]
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0]==23
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==23


def freeze_as_deployed_predecessor(settings,batch_id,revision):
    with Store(settings.database) as store:
        row=store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone()
        data=json.loads(row[0]);data['runtime_revision']=revision
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch_id))
        for job in store.conn.execute('SELECT id,request_json FROM pipeline_jobs WHERE batch_id=?',(batch_id,)).fetchall():
            request=json.loads(job['request_json']);request['runtime_revision']=revision
            store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',(encode(request),job['id']))


@pytest.mark.parametrize('revision',sorted(p.EXECUTION_COMPATIBLE_REVISIONS))
def test_tested_compatible_upgrade_settles_accepted_jobs_without_reissuing(settings, monkeypatch, revision):
    ingest(settings)
    native=p.settle
    monkeypatch.setattr(p,'settle',lambda *args:(_ for _ in ()).throw(RuntimeError('synthetic process interruption')))
    with pytest.raises(RuntimeError,match='process interruption'):
        asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    monkeypatch.setattr(p,'settle',native)
    with Store(settings.database,read_only=True) as store:
        batch_id=store.conn.execute('SELECT id FROM pipeline_batches').fetchone()[0]
    freeze_as_deployed_predecessor(settings,batch_id,revision)
    with Store(settings.database,read_only=True) as store:
        before=[tuple(r) for r in store.conn.execute('SELECT * FROM pipeline_jobs ORDER BY id')]
        raw=[tuple(r) for r in store.conn.execute('SELECT * FROM raw_events')]
    p.initialize(settings.database)
    async def forbidden(*args):pytest.fail('A compatible accepted job was reissued')
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))
    assert result['status']=='processed' and result['events']==1
    with Store(settings.database,read_only=True) as store:
        after=[tuple(r) for r in store.conn.execute('SELECT * FROM pipeline_jobs ORDER BY id')]
        assert [(r[0],r[-1]) for r in after]==[(r[0],r[-1]) for r in before]
        assert [tuple(r) for r in store.conn.execute('SELECT * FROM raw_events')]==raw
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==2
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='current'


@pytest.mark.parametrize('revision',[*sorted(p.EXECUTION_COMPATIBLE_REVISIONS),'unknown-protocol'])
def test_upgrade_preserves_original_requests_and_failure_budget(settings,revision):
    ingest(settings)
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    with Store(settings.database) as store:
        data=json.loads(batch['input_json'])
        data['runtime_revision']=revision
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch['id']))
        request={'role':'track_router','contract':p.CONTRACT,'runtime_revision':data['runtime_revision'],'prompt':'frozen original'}
        store.conn.execute('INSERT INTO pipeline_jobs VALUES (?,?,?,?,NULL)',('frozen-job',batch['id'],'track_router:0',encode(request)))
        store.conn.execute("INSERT INTO pipeline_job_failures VALUES ('frozen-job',2,'synthetic timeout')")
        original=store.conn.execute("SELECT request_json FROM pipeline_jobs WHERE id='frozen-job'").fetchone()[0]
    p.initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT failures FROM pipeline_job_failures WHERE job_id='frozen-job'").fetchone()[0]==2
        assert store.conn.execute("SELECT request_json FROM pipeline_jobs WHERE id='frozen-job'").fetchone()[0]==original
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()[0]==('pending' if revision in p.EXECUTION_COMPATIBLE_REVISIONS else 'needs_repair')


@pytest.mark.parametrize('revision',sorted(p.EXECUTION_COMPATIBLE_REVISIONS))
def test_unknown_contract_is_never_accepted_by_known_revision(revision):
    assert not p.runtime_compatible({'contract':'unknown','runtime_revision':revision})


def fail_real_stage_twice(settings,unfinished_role):
    ingest(settings)
    async def fail(role,request):
        if role==unfinished_role:raise OSError('synthetic pre-upgrade outage')
        return output_for(role,request)
    for _ in range(2):
        with pytest.raises(OSError,match='pre-upgrade outage'):
            asyncio.run(p.advance(settings.database,include_recent=True,runner=fail))
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute('SELECT id,batch_id FROM pipeline_jobs WHERE output_json IS NULL').fetchone()
        assert p.stage_failures(settings.database,row['id'])==2
        return row['batch_id'],row['id']


@pytest.mark.parametrize('revision',sorted(p.EXECUTION_COMPATIBLE_REVISIONS))
@pytest.mark.parametrize('unfinished_role',p.ROLES)
def test_compatible_upgrade_resumes_only_unfinished_roles(settings,monkeypatch,revision,unfinished_role):
    batch_id,job_id=fail_real_stage_twice(settings,unfinished_role)
    freeze_as_deployed_predecessor(settings,batch_id,revision)
    with Store(settings.database,read_only=True) as store:
        originals=[tuple(row) for row in store.conn.execute('SELECT * FROM raw_events')]
        frozen={row['id']:(row['request_json'],row['output_json']) for row in
                store.conn.execute('SELECT * FROM pipeline_jobs')}
    p.initialize(settings.database)
    assert p.stage_failures(settings.database,job_id)==2
    called=[]
    async def finish(role,request):
        called.append(role)
        if role==unfinished_role:
            assert encode(request)==frozen[job_id][0]
        return output_for(role,request)
    native=p.settle
    def verify_before_compaction(database,batch,data,routed,plans):
        with Store(database,read_only=True) as store:
            for row in store.conn.execute('SELECT * FROM pipeline_jobs'):
                if row['id'] in frozen:
                    request,output=frozen[row['id']]
                    assert row['request_json']==request
                    if output is not None:assert row['output_json']==output
        return native(database,batch,data,routed,plans)
    monkeypatch.setattr(p,'settle',verify_before_compaction)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=finish))
    assert result['status']=='processed' and result['events']==1
    assert called==list(p.ROLES[p.ROLES.index(unfinished_role):])
    assert p.stage_failures(settings.database,job_id)==2
    with Store(settings.database,read_only=True) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM raw_events')]==originals
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==2
        assert store.conn.execute('SELECT count(*) FROM pipeline_batches').fetchone()[0]==1
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=finish))['status']=='current'
    assert called==list(p.ROLES[p.ROLES.index(unfinished_role):])


@pytest.mark.parametrize('revision',sorted(p.EXECUTION_COMPATIBLE_REVISIONS))
@pytest.mark.parametrize('unfinished_role',p.ROLES)
def test_compatible_upgrade_allows_only_remaining_api_attempt(settings,monkeypatch,revision,unfinished_role):
    batch_id,job_id=fail_real_stage_twice(settings,unfinished_role)
    freeze_as_deployed_predecessor(settings,batch_id,revision)
    with Store(settings.database,read_only=True) as store:
        accepted={row['id']:row['output_json'] for row in
                  store.conn.execute('SELECT * FROM pipeline_jobs WHERE output_json IS NOT NULL')}
        originals=[tuple(row) for row in store.conn.execute('SELECT * FROM raw_events')]
    def fail(request):
        assert request['role']==unfinished_role
        raise OSError('synthetic final attempt failed')
    calls=api(settings,monkeypatch,fail)
    p.initialize(settings.database)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert result['status']=='retained',result
    assert len(calls)==1 and p.stage_failures(settings.database,job_id)==3
    for _ in range(2):
        p.initialize(settings.database)
        asyncio.run(p.scheduled_advance(settings.database))
    assert len(calls)==1 and p.stage_failures(settings.database,job_id)==3
    with Store(settings.database,read_only=True) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM raw_events')]==originals
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone()[0]=='waiting_materials'
        assert {row['id']:row['output_json'] for row in store.conn.execute(
            'SELECT * FROM pipeline_jobs WHERE output_json IS NOT NULL')}==accepted
        assert store.conn.execute("SELECT count(*) FROM pipeline_pending_materials WHERE batch_id=? AND status='open'",(batch_id,)).fetchone()[0]==1
