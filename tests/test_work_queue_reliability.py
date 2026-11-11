"""Synthetic restart and cross-key fairness checks for durable requested work."""
import asyncio
import json
import sqlite3
import threading
import time

import pytest

from test_public_features import settings
from serein import work_tasks as work
from serein.core.store import Store


def save_state(settings, key, **fields):
    with Store(settings.database) as store:
        value=work._get(store,key)
        value.update(fields)
        work._save(store,key,value)


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


def test_requested_pipeline_resumes_after_worker_restart_without_losing_arguments(settings,monkeypatch):
    calls=[]
    async def run():
        started=asyncio.Event()
        async def first(settings,key,arguments):
            calls.append((key,arguments.copy()))
            work.progress(stage='event_writer',batch_id='frozen',job_id='frozen:writer',completed=2)
            started.set()
            await asyncio.Future()
        monkeypatch.setattr(work,'work',first)
        queued=work.enqueue(settings.database,'pipeline',{'include_recent':True})
        worker=asyncio.create_task(work.run(settings))
        await wait_for(started.is_set)
        worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        interrupted=work.status(settings.database,'pipeline')
        assert interrupted['status']=='interrupted'
        assert interrupted['completed']==2 and interrupted['job_id']=='frozen:writer'
        async def finish(settings,key,arguments):
            calls.append((key,arguments.copy()))
            return {'status':'processed','events':1}
        monkeypatch.setattr(work,'work',finish)
        worker=asyncio.create_task(work.run(settings))
        try:
            await wait_for(lambda:work.status(settings.database,'pipeline')['status']=='completed')
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        state=work.status(settings.database,'pipeline')
        assert state['resumed_from_run_id']==queued['run_id']
        assert calls==[('pipeline',{'include_recent':True}),('pipeline',{'include_recent':True})]
    asyncio.run(run())


@pytest.mark.parametrize('key',['pipeline','import:synthetic'])
def test_expired_requested_lease_resumes_only_once_and_preserves_new_followup(settings,key):
    queued=work.enqueue(settings.database,key,{'include_recent':False})
    save_state(settings,key,status='running',lease_until=0,resume_on_restart=True,
               followup_arguments={'include_recent':True},completed=3,job_id='frozen:curator')
    work.recover_requested_work(settings.database)
    resumed=work.status(settings.database,key)
    assert resumed['status']=='queued' and resumed['run_id']!=queued['run_id']
    assert resumed['arguments']=={'include_recent':False}
    assert resumed['followup_arguments']=={'include_recent':True}
    assert resumed['completed']==3 and resumed['job_id']=='frozen:curator'
    work.recover_requested_work(settings.database)
    assert work.status(settings.database,key)['run_id']==resumed['run_id']


@pytest.mark.parametrize('state,extra',[
    ('running',{'lease_until':time.time()+3600}),
    ('running',{'lease_until':0,'stop_requested':True}),
    ('paused',{}),('failed',{}),('needs_repair',{}),('awaiting_agent',{}),
])
def test_restart_never_steals_live_lease_or_revives_explicit_holds(settings,state,extra):
    queued=work.enqueue(settings.database,'pipeline',{'include_recent':True})
    save_state(settings,'pipeline',status=state,resume_on_restart=True,**extra)
    work.recover_requested_work(settings.database)
    after=work.status(settings.database,'pipeline')
    assert after['run_id']==queued['run_id'] and after['status']!='queued'


def test_pause_while_interrupted_prevents_restart_resumption(settings):
    queued=work.enqueue(settings.database,'import:interrupted-pause')
    save_state(settings,queued['id'],status='running',lease_until=0,resume_on_restart=True)
    assert work.pause(settings.database,queued['id'])['status']=='paused'
    work.recover_requested_work(settings.database)
    assert work.status(settings.database,queued['id'])['status']=='paused'


def test_scheduled_or_legacy_running_work_is_not_promoted_to_requested_continue(settings):
    save_state(settings,'pipeline',status='running',lease_until=0,
               arguments={'include_recent':True},run_id='old-scheduled')
    work.recover_requested_work(settings.database)
    assert work.status(settings.database,'pipeline')['status']=='interrupted'


def test_timer_pass_does_not_inherit_finished_manual_request_restart_intent(settings):
    async def run():
        queued=work.enqueue(settings.database,'pipeline',{'include_recent':True})
        async def finish():return {'status':'processed'}
        await work.execute(settings.database,'pipeline',finish,queued_id=queued['run_id'])
        started=asyncio.Event()
        async def timer():
            started.set();await asyncio.Future()
        task=asyncio.create_task(work.execute(settings.database,'pipeline',timer))
        await started.wait();task.cancel();await asyncio.gather(task,return_exceptions=True)
        work.recover_requested_work(settings.database)
        assert work.status(settings.database,'pipeline')['status']=='interrupted'
        assert not work.status(settings.database,'pipeline')['resume_on_restart']
    asyncio.run(run())


def test_startup_timer_cannot_consume_interrupted_explicit_continuation(settings):
    queued=work.enqueue(settings.database,'pipeline',{'include_recent':True})
    save_state(settings,'pipeline',status='running',lease_until=0,resume_on_restart=True)
    async def forbidden():pytest.fail('Timer stole interrupted explicit request')
    result=asyncio.run(work.execute(settings.database,'pipeline',forbidden))
    assert result['status']=='busy'
    resumed=work.status(settings.database,'pipeline')
    assert resumed['status']=='queued' and resumed['arguments']=={'include_recent':True}
    assert resumed['resumed_from_run_id']==queued['run_id']
    async def finish():return {'status':'processed','events':1}
    result=asyncio.run(work.execute(settings.database,'pipeline',finish,queued_id=resumed['run_id']))
    assert result['events']==1 and work.status(settings.database,'pipeline')['status']=='completed'


def test_import_restart_finishes_checkpoint_without_reinserting_earlier_rows(settings,monkeypatch):
    from serein import imports
    from test_file_imports import messages
    upload=imports.stage(settings.database,json.dumps(messages(52)),'restart.json','auto',False)
    original=imports.advance_import
    committed=threading.Event();release=threading.Event();calls=[]
    def checkpoint(*args,**kwargs):
        result=original(*args,**kwargs);calls.append(result['processed'])
        if len(calls)==1:
            committed.set()
            assert release.wait(5),'Test did not release its committed disk batch'
        return result
    monkeypatch.setattr(imports,'advance_import',checkpoint)
    async def run():
        key='import:'+upload['id']
        work.enqueue(settings.database,key)
        worker=asyncio.create_task(work.run(settings))
        await wait_for(committed.is_set)
        worker.cancel()
        release.set()
        await asyncio.gather(worker,return_exceptions=True)
        assert work.status(settings.database,key)['status']=='interrupted'
        assert imports.list_imports(settings.database)[0]['processed']==25
        worker=asyncio.create_task(work.run(settings))
        try:
            await wait_for(lambda:work.status(settings.database,key)['status']=='completed')
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        row=imports.list_imports(settings.database)[0]
        assert row['processed']==row['inserted']==52
        assert calls==[25,50,52]
        with Store(settings.database,read_only=True) as store:
            assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==52
    try:asyncio.run(run())
    finally:release.set()


def test_pipeline_restart_reuses_saved_router_and_settles_sources_once(settings,monkeypatch):
    from importlib import import_module
    from test_public_features import ingest,output_for
    from serein.deployment import save_settings
    from serein.extensions import pipeline as p
    for module in ('serein.image_transcription','serein.api.chat'):import_module(module)
    ingest(settings)
    save_settings(settings.database,{'models':[{'id':'stub','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{role:'stub' for role in p.ROLES}})
    calls=[]
    async def run():
        curator_started=asyncio.Event()
        async def complete(model,payload):
            with Store(settings.database,read_only=True) as store:
                request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
            calls.append(request['role'])
            if request['role']=='event_curator' and calls.count('event_curator')==1:
                curator_started.set();await asyncio.Future()
            return {'choices':[{'message':{'content':json.dumps(output_for(request['role'],request))}}]}
        monkeypatch.setattr('serein.model_runtime.complete',complete)
        work.enqueue(settings.database,'pipeline',{'include_recent':True})
        worker=asyncio.create_task(work.run(settings))
        try:
            await wait_for(curator_started.is_set)
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        assert calls==['track_router','event_curator']
        with Store(settings.database,read_only=True) as store:
            router=[tuple(r) for r in store.conn.execute("SELECT * FROM pipeline_jobs WHERE role LIKE 'track_router:%'")]
            assert len(router)==1 and router[0][-1] is not None
        worker=asyncio.create_task(work.run(settings))
        try:
            await wait_for(lambda:work.status(settings.database,'pipeline')['status']=='completed')
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        assert calls==['track_router','event_curator','event_curator','event_writer']
        with Store(settings.database,read_only=True) as store:
            assert [tuple(r) for r in store.conn.execute("SELECT * FROM pipeline_jobs WHERE role LIKE 'track_router:%'")]==router
            assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==2
            assert store.conn.execute('SELECT count(*) FROM fact_event_settlement_operations').fetchone()[0]==1
            assert store.conn.execute('SELECT count(*) FROM pipeline_job_failures').fetchone()[0]==0
    asyncio.run(run())


def test_explicit_pause_stops_pipeline_between_batches_and_does_not_auto_resume(settings,monkeypatch):
    from serein.extensions import pipeline as p
    calls=[]
    async def advance(database,**kwargs):
        calls.append(True)
        work.pause(database,'pipeline')
        return {'status':'processed','events':1,'processed_originals':2}
    monkeypatch.setattr(p,'_advance',advance)
    queued=work.enqueue(settings.database,'pipeline',{'include_recent':True})
    result=asyncio.run(work.execute(settings.database,'pipeline',
        lambda:work.work(settings,'pipeline',queued['arguments']),queued_id=queued['run_id']))
    assert result['status']=='paused' and result['events']==1 and calls==[True]
    work.recover_requested_work(settings.database)
    assert work.status(settings.database,'pipeline')['status']=='paused'


def test_requested_worker_does_not_serialize_independent_import_behind_long_pipeline(settings,monkeypatch):
    async def run():
        pipeline_started=asyncio.Event();release=asyncio.Event();import_finished=asyncio.Event()
        async def operation(settings,key,arguments):
            if key=='pipeline':
                pipeline_started.set();await release.wait()
            else:
                assert key=='import:independent'
                assert pipeline_started.is_set() and not release.is_set()
                import_finished.set()
            return {'status':'completed'}
        monkeypatch.setattr(work,'work',operation)
        work.enqueue(settings.database,'pipeline',{'include_recent':True})
        worker=asyncio.create_task(work.run(settings))
        try:
            await wait_for(pipeline_started.is_set)
            work.enqueue(settings.database,'import:independent')
            await wait_for(import_finished.is_set)
            assert work.status(settings.database,'pipeline')['status']=='running'
            release.set()
            await wait_for(lambda:work.status(settings.database,'pipeline')['status']=='completed')
        finally:
            release.set();worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(run())


def test_two_workers_share_durable_lease_and_keep_concurrency_bounded(settings,monkeypatch):
    async def run():
        active=set();seen=[];release=asyncio.Event()
        async def operation(settings,key,arguments):
            assert key not in active,'A shared queue key was executed twice'
            active.add(key);seen.append(key)
            assert len(active)<=2*work.REQUESTED_WORK_CONCURRENCY
            await release.wait()
            active.remove(key)
            return {'status':'completed'}
        monkeypatch.setattr(work,'work',operation)
        for index in range(7):work.enqueue(settings.database,'import:'+str(index))
        workers=[asyncio.create_task(work.run(settings)) for _ in range(2)]
        try:
            await wait_for(lambda:len(active)==2*work.REQUESTED_WORK_CONCURRENCY)
            assert len(seen)==len(set(seen))
            release.set()
            await wait_for(lambda:len(seen)==7 and not active)
            assert sorted(seen)==['import:'+str(index) for index in range(7)]
        finally:
            release.set()
            for worker in workers:worker.cancel()
            await asyncio.gather(*workers,return_exceptions=True)
    asyncio.run(run())


def test_queue_scan_recovers_after_transient_database_error_without_requeuing_jobs(settings,monkeypatch):
    original=work.recover_image_work;scans=[];calls=[]
    def once(database):
        scans.append(True)
        if len(scans)==1:raise sqlite3.OperationalError('Synthetic busy database')
        return original(database)
    async def operation(settings,key,arguments):
        calls.append(key);return {'status':'completed'}
    monkeypatch.setattr(work,'recover_image_work',once)
    monkeypatch.setattr(work,'QUEUE_SCAN_RETRY_SECONDS',.01)
    monkeypatch.setattr(work,'work',operation)
    queued=work.enqueue(settings.database,'import:after-scan-error')
    async def run():
        worker=asyncio.create_task(work.run(settings))
        try:await wait_for(lambda:work.status(settings.database,queued['id'])['status']=='completed')
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(run())
    assert len(scans)>=2 and calls==[queued['id']]


def test_configuration_scan_recovers_without_terminating_model_job_host(settings,monkeypatch):
    from types import SimpleNamespace
    from serein import model_jobs
    original=model_jobs.read_settings;scans=[]
    def once(database):
        scans.append(True)
        if len(scans)==1:raise sqlite3.OperationalError('Synthetic busy settings')
        return original(database)
    monkeypatch.setattr(model_jobs,'read_settings',once)
    monkeypatch.setattr(model_jobs,'CONFIGURATION_RETRY_SECONDS',.01)
    async def run():
        host=asyncio.create_task(model_jobs.run(SimpleNamespace(settings=settings,
            enabled_extensions={'relations','dreams','narrative_revision','event_pipeline','operit_tagging','arc_linker'})))
        try:
            await wait_for(lambda:len(scans)>=2)
            assert not host.done()
        finally:
            host.cancel();await asyncio.gather(host,return_exceptions=True)
    asyncio.run(run())


def test_heartbeat_retries_transient_write_failure_without_cancelling_owned_work(settings,monkeypatch):
    original=work._renew_lease;renewals=[]
    monkeypatch.setattr(work,'HEARTBEAT_SECONDS',.03)
    monkeypatch.setattr(work,'HEARTBEAT_RETRY_SECONDS',.01)
    def once(*args):
        renewals.append(True)
        if len(renewals)==1:raise sqlite3.OperationalError('Synthetic renewal busy')
        return original(*args)
    monkeypatch.setattr(work,'_renew_lease',once)
    async def run():
        async def operation():
            await wait_for(lambda:len(renewals)>=2)
            return {'status':'processed','events':1}
        queued=work.enqueue(settings.database,'pipeline')
        result=await work.execute(settings.database,'pipeline',operation,queued_id=queued['run_id'])
        assert result['events']==1
        assert work.status(settings.database,'pipeline')['status']=='completed'
    asyncio.run(run())


def test_lost_lease_cancels_old_operation_without_overwriting_new_owner(settings,monkeypatch):
    monkeypatch.setattr(work,'HEARTBEAT_SECONDS',.03)
    async def run():
        started=asyncio.Event();cancelled=asyncio.Event()
        async def operation():
            started.set()
            try:await asyncio.Future()
            finally:cancelled.set()
        queued=work.enqueue(settings.database,'pipeline')
        task=asyncio.create_task(work.execute(settings.database,'pipeline',operation,queued_id=queued['run_id']))
        await started.wait()
        save_state(settings,'pipeline',run_id='replacement-owner',status='running',lease_until=time.time()+60)
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,2)
        assert cancelled.is_set()
        state=work.status(settings.database,'pipeline')
        assert state['status']=='running' and state['run_id']=='replacement-owner'
    asyncio.run(run())


def test_heartbeat_cancels_before_last_confirmed_lease_expires(settings,monkeypatch):
    monkeypatch.setattr(work,'LEASE_SECONDS',1)
    monkeypatch.setattr(work,'HEARTBEAT_SECONDS',.03)
    monkeypatch.setattr(work,'HEARTBEAT_RETRY_SECONDS',.01)
    def unavailable(*args):raise sqlite3.OperationalError('Synthetic persistent renewal busy')
    monkeypatch.setattr(work,'_renew_lease',unavailable)
    async def run():
        saved_deadline=[]
        async def operation():
            saved_deadline.append(work.status(settings.database,'pipeline')['lease_until'])
            await asyncio.Future()
        queued=work.enqueue(settings.database,'pipeline')
        with pytest.raises(asyncio.CancelledError):
            await work.execute(settings.database,'pipeline',operation,queued_id=queued['run_id'])
        assert time.time()<saved_deadline[0]
        state=work.status(settings.database,'pipeline')
        assert state['status']=='interrupted' and state['lease_until']==0
    asyncio.run(run())


def test_cancellation_remains_cancellation_when_interrupted_status_write_fails(settings,monkeypatch):
    original=work.progress
    def broken_status(**fields):
        if fields.get('status')=='interrupted':raise sqlite3.OperationalError('Synthetic shutdown busy')
        return original(**fields)
    monkeypatch.setattr(work,'progress',broken_status)
    async def run():
        started=asyncio.Event()
        async def operation():started.set();await asyncio.Future()
        task=asyncio.create_task(work.execute(settings.database,'pipeline',operation))
        await started.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert work.status(settings.database,'pipeline')['status']=='running'
    asyncio.run(run())


def test_stalled_event_loop_cannot_overlap_replacement_after_lease_expiry(settings):
    started=threading.Event();release=threading.Event();finished=[];replacement_calls=[]
    queued=work.enqueue(settings.database,'pipeline',{'include_recent':True})
    async def stalled():
        started.set()
        assert release.wait(5),'Test did not release the blocked event loop'
        return {'status':'processed'}
    def owner():
        finished.append(asyncio.run(work.execute(settings.database,'pipeline',stalled,queued_id=queued['run_id'])))
    thread=threading.Thread(target=owner)
    thread.start()
    try:
        assert started.wait(5)
        save_state(settings,'pipeline',lease_until=0)
        work.recover_requested_work(settings.database)
        replacement=work.status(settings.database,'pipeline')
        assert replacement['status']=='queued' and replacement['run_id']!=queued['run_id']
        async def next_owner():
            replacement_calls.append(True)
            return {'status':'processed'}
        result=asyncio.run(work.execute(settings.database,'pipeline',next_owner,queued_id=replacement['run_id']))
        assert result['status']=='busy' and not replacement_calls
    finally:
        release.set();thread.join(5)
    assert not thread.is_alive() and finished==[{'status':'processed'}]
    result=asyncio.run(work.execute(settings.database,'pipeline',next_owner,queued_id=replacement['run_id']))
    assert result['status']=='processed' and replacement_calls==[True]


def test_import_keeps_exclusion_during_expired_lease_and_repeated_cancel(settings,monkeypatch):
    from serein import imports
    from test_file_imports import messages
    upload=imports.stage(settings.database,json.dumps(messages(52)),'cancel-fence.json','auto',False)
    original=imports.advance_import
    committed=threading.Event();release=threading.Event();calls=[];replacement_calls=[]
    def checkpoint(*args,**kwargs):
        result=original(*args,**kwargs);calls.append(result['processed'])
        if len(calls)==1:
            committed.set()
            assert release.wait(5),'Test did not release the committed disk batch'
        return result
    monkeypatch.setattr(imports,'advance_import',checkpoint)
    async def run():
        key='import:'+upload['id']
        queued=work.enqueue(settings.database,key)
        task=asyncio.create_task(work.execute(settings.database,key,lambda:work.work(settings,key,{}),queued_id=queued['run_id']))
        try:
            await wait_for(committed.is_set)
            task.cancel();await asyncio.sleep(.01)
            save_state(settings,key,lease_until=0)
            work.recover_requested_work(settings.database)
            replacement=work.status(settings.database,key)
            async def forbidden():
                replacement_calls.append(True)
                return {'status':'completed'}
            for _ in range(2):
                result=await work.execute(settings.database,key,forbidden,queued_id=replacement['run_id'])
                assert result['status']=='busy' and not replacement_calls
                task.cancel();await asyncio.sleep(.01)
                assert not task.done(),'Cancelled wrapper released a still-running disk batch'
        finally:
            release.set()
            await asyncio.gather(task,return_exceptions=True)
        assert task.cancelled()
        result=await work.execute(settings.database,key,lambda:work.work(settings,key,{}),queued_id=replacement['run_id'])
        assert result['status']=='completed'
        assert calls==[25,50,52]
        with Store(settings.database,read_only=True) as store:
            assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==52
    try:asyncio.run(run())
    finally:release.set()
