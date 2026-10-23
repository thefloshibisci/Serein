import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from serein.compat.germany.scene_linker import SceneLinker
from serein.compat.jobs import update_scene_jobs, scene_job_failures, retry_scene_job
from serein.compat.scout import Scout
from serein.compat.narratives import narrative_transaction, RevisionInbox
from serein.compat.scenes import Scenes
from serein.application import Application
from serein.core.store import Store
from test_live_clients import live
from test_live_relations import seed
from test_live_narratives import seed as seed_narrative
from test_public_features import settings as idle_settings


def test_scene_job_survives_failure_and_concurrent_edit(live):
    settings, client = live
    source, target, _ = seed(client)
    linker = SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True}})
    linker.handle_scene_content_changed = AsyncMock(return_value={'normal_relink_required':True})
    linker.link_scene = AsyncMock(return_value={'status':'failed'})
    assert asyncio.run(update_scene_jobs(settings,linker))['failed'] == 2
    assert linker.link_scene.await_count == 2
    for _ in range(3):
        asyncio.run(update_scene_jobs(settings,linker))
    assert linker.link_scene.await_count == 2
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM scene_jobs').fetchone()[0] == 2

    for job in scene_job_failures(settings):
        assert retry_scene_job(settings,job['scene_id'],job['attempt_id'])['status']=='queued'
        assert retry_scene_job(settings,job['scene_id'],job['attempt_id'])['status']=='conflict'

    async def write_during_model(*args):
        with Store(settings.database) as store:
            store.conn.execute('INSERT INTO scene_jobs(scene_id) VALUES(?)',(source,))
        return {'status':'no_edges'}

    linker.link_scene = AsyncMock(side_effect=write_during_model)
    asyncio.run(update_scene_jobs(settings,linker))
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM scene_jobs').fetchone()[0] == 2
    linker.link_scene = AsyncMock(return_value={'status':'no_edges'})
    asyncio.run(update_scene_jobs(settings,linker))
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM scene_jobs').fetchone()[0] == 0


@pytest.mark.parametrize('failure',['contract','exception','cancelled'])
def test_scene_failure_blocks_restart_but_new_content_can_run(live,failure):
    settings,client=live
    source,target,_=seed(client)
    linker=SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True}})
    linker.handle_scene_content_changed=AsyncMock(return_value={'normal_relink_required':True})
    async def run(key,*args):
        if key==source:
            if failure=='exception':raise ValueError('Synthetic evidence binding failed')
            if failure=='cancelled':raise asyncio.CancelledError
            return {'status':'failed','attempts':[{'status':'invalid_json_contract','model':'synthetic'}]}
        return {'status':'no_edges'}
    linker.link_scene=AsyncMock(side_effect=run)
    if failure=='cancelled':
        with pytest.raises(asyncio.CancelledError):asyncio.run(update_scene_jobs(settings,linker))
    else:asyncio.run(update_scene_jobs(settings,linker))
    # A fresh worker object still obeys the on-disk stop; other Scenes proceed.
    restarted=SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True}})
    restarted.handle_scene_content_changed=linker.handle_scene_content_changed
    restarted.link_scene=AsyncMock(return_value={'status':'no_edges'})
    asyncio.run(update_scene_jobs(settings,restarted))
    assert all(call.args[0]!=source for call in restarted.link_scene.await_args_list)
    failures=scene_job_failures(settings)
    assert len(failures)==1 and failures[0]['scene_id']==source
    error_view=client.get('/api/scene-edge-proposals?status=error').json()
    assert error_view['count']==1 and error_view['proposals']==[]
    assert error_view['failed_jobs'][0]['scene_id']==source
    if failure=='contract':
        assert failures[0]['attempts'][0]['status']=='invalid_json_contract'
        assert '格式' in failures[0]['error']
    with Store(settings.database) as store,store.transaction(immediate=True):
        doc=store.read(source)
        store.revise(source,expected_revision=doc['revision'],title=doc['title'],body_md='Synthetic changed content.')
    restarted.link_scene.reset_mock()
    asyncio.run(update_scene_jobs(settings,restarted))
    assert restarted.link_scene.await_count==1 and restarted.link_scene.call_args.args[0]==source
    assert scene_job_failures(settings)==[]


def test_scene_retry_endpoint_is_explicit_and_rejects_duplicate_requests(live):
    from serein.deployment import save_settings
    settings,client=live
    source,target,_=seed(client)
    linker=SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True}})
    linker.handle_scene_content_changed=AsyncMock(return_value={'normal_relink_required':True})
    linker.link_scene=AsyncMock(return_value={'status':'failed','attempts':[{'status':'evidence_contract_failed'}]})
    asyncio.run(update_scene_jobs(settings,linker))
    job=client.get('/api/scene-edge-proposals').json()['failed_jobs'][0]
    body={key:job[key] for key in ('scene_id','attempt_id')}
    assert client.post('/api/scene-relation-jobs/retry',json=body).status_code==409
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}], 'assignments':{'relations':'local'}})
    assert client.post('/api/scene-relation-jobs/retry',json=body).json()['status']=='queued'
    assert client.post('/api/scene-relation-jobs/retry',json=body).status_code==409
    linker.link_scene.reset_mock()
    asyncio.run(update_scene_jobs(settings,linker))
    asyncio.run(update_scene_jobs(settings,linker))
    assert linker.link_scene.await_count==1
    assert client.post('/api/scene-relation-jobs/retry',json=body).status_code==409


def test_blocked_scene_jobs_do_not_starve_later_scenes(idle_settings):
    settings=idle_settings
    with Store(settings.database) as store:
        for i in range(102):
            store.create(f'scene-{i:03}','scene','Synthetic','Synthetic content.')
    linker=SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True}})
    linker.handle_scene_content_changed=AsyncMock(return_value={'normal_relink_required':True})
    linker.link_scene=AsyncMock(return_value={'status':'failed'})
    asyncio.run(update_scene_jobs(settings,linker))
    assert linker.link_scene.await_count==100
    asyncio.run(update_scene_jobs(settings,linker))
    assert linker.link_scene.await_count==102
    asyncio.run(update_scene_jobs(settings,linker))
    assert linker.link_scene.await_count==102


def test_invalid_provider_response_is_called_only_once_per_scene(live):
    from serein.compat.background import SceneReader
    settings,client=live
    source,target,_=seed(client)
    linker=SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True}})
    linker.providers=[{'name':'synthetic','model':'synthetic','client':object()}]
    linker.handle_scene_content_changed=AsyncMock(return_value={'normal_relink_required':True})
    reader=SceneReader(settings.database)
    async def candidates(anchor,*args):
        return [await reader.get(target if anchor['id']==source else source)]
    linker._candidate_scenes=candidates
    linker._call_provider=AsyncMock(return_value={'missing_edges':[]})
    for _ in range(3):asyncio.run(update_scene_jobs(settings,linker))
    assert linker._call_provider.await_count==2
    assert all(job['attempts'][0]['status']=='invalid_json_contract' for job in scene_job_failures(settings))


@pytest.mark.parametrize('valid',[False,True])
def test_scene_response_accepts_valid_edges_without_rejecting_whole_response(live,valid):
    from serein.compat.background import SceneReader
    from test_live_relations import create
    settings,client=live
    source=create(client,'Synthetic A','A synthetic shared moment beside the window.')
    target=create(client,'Synthetic B','Another synthetic memory beside the window.')
    reader=SceneReader(settings.database)
    anchor=asyncio.run(reader.get(source));candidate=asyncio.run(reader.get(target))
    linker=SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True,'min_confidence':0.99}})
    linker.providers=[{'name':'synthetic','model':'synthetic','client':object()}]
    linker._candidate_scenes=AsyncMock(return_value=[candidate])
    edge={'candidate_scene_id':target,'relation_type':'echoes','orientation':'symmetric','confidence':0.0,
          'reason':'雨声回响',
          'new_scene_evidence':'A','candidate_scene_evidence':'A'}
    invalid={**edge,'candidate_scene_id':'not-an-allowed-scene'}
    linker._call_provider=AsyncMock(return_value={'edges':([edge] if valid else [])+[invalid]})
    result=asyncio.run(linker.link_scene(source,reader))
    assert result['status']==('proposed' if valid else 'failed')
    assert result.get('proposal_count',0)==(1 if valid else 0)
    assert result['attempts'][0]['rejections']==[{'reason':'candidate_not_allowed','candidate':'not-an-allowed-scene'}]
    assert linker._call_provider.await_count==1
    if valid:
        proposals=client.get('/api/scene-edge-proposals').json()['proposals']
        assert len(proposals)==1,proposals
        assert proposals[0]['confidence']==0.0
        accepted=client.post('/api/scene-edge-proposals/review',json={
            'proposal_id':proposals[0]['proposal_id'],'decision':'accept','confirm':'ACCEPT_SCENE_EDGE'})
        assert accepted.status_code==200,accepted.text
        assert accepted.json()['status']=='accepted',accepted.text
        assert len(client.get('/api/scene-edges').json()['edges'])==1


def test_scene_worker_and_manual_retry_cannot_duplicate_inflight_request(live):
    settings,client=live
    source,target,_=seed(client)
    linker=SceneLinker({'serein_database':str(settings.database),'scene_linker':{'enabled':True,'auto_enabled':True}})
    linker.handle_scene_content_changed=AsyncMock(return_value={'normal_relink_required':True})
    async def check():
        started=asyncio.Event();release=asyncio.Event()
        async def provider(*args):
            started.set();await release.wait();return {'status':'failed'}
        linker.link_scene=AsyncMock(side_effect=provider)
        task=asyncio.create_task(update_scene_jobs(settings,linker))
        try:
            await asyncio.wait_for(started.wait(),2)
            assert (await update_scene_jobs(settings,linker))['status']=='busy'
            job=scene_job_failures(settings)[0]
            with pytest.raises(RuntimeError):retry_scene_job(settings,job['scene_id'],job['attempt_id'])
        finally:
            release.set();await task
        assert linker.link_scene.await_count==2
    asyncio.run(check())
    asyncio.run(update_scene_jobs(settings,linker))
    assert linker.link_scene.await_count==2


def test_scout_retains_scan_checkpoint_across_restart(live,tmp_path):
    settings,client=live
    seed(client)
    config=tmp_path/'germany.yaml'
    config.write_text('narrative_rolls:\n  revision_scan_enabled: true\n  new_roll_scout_enabled: false\n','utf-8')
    settings=replace(settings,background={'germany_config_file':str(config)})
    scout=Scout(settings)
    result=asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert result['checked_rolls']==0
    restarted=Scout(settings)
    assert restarted.inbox.scan_metadata()['checked_rolls']==0
    stamp=datetime.fromisoformat(restarted.inbox.scan_metadata()['last_scan_at']).astimezone(ZoneInfo('Asia/Shanghai'))
    assert asyncio.run(restarted.run_due(stamp.replace(hour=23)))['status']=='not_due'


def test_revision_background_starts_without_a_selected_model(live, monkeypatch):
    from serein.application import Application
    from serein import model_jobs
    from serein.compat import jobs as background_jobs

    settings, _ = live
    started = asyncio.Event()
    async def observed_run(self):
        if self.scout is not None:
            started.set()
        await asyncio.Future()

    monkeypatch.setattr(background_jobs.BackgroundJobs, 'run', observed_run)

    async def check():
        task = asyncio.create_task(model_jobs.run(Application(settings)))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


def test_promoted_event_leaves_revision_inbox_and_future_scout_materials(live):
    settings, client = live
    from serein.deployment import save_settings
    save_settings(settings.database, {'features': {'event_to_scene': True}})
    event_id, _ = seed_narrative(client, settings)
    with narrative_transaction(settings.database, write=True) as rolls:
        RevisionInbox(rolls.store)._save({'items': [{
            'proposal_id': 'old-event-hint', 'proposal_kind': 'new_roll_candidate',
            'narrative_id': '', 'status': 'pending', 'source_event_ids': [event_id]}]})
    before = Scout(settings)
    assert any(item['source_type'] == 'event' and item['source_id'] == event_id
               for item in asyncio.run(before._active_narrative_material_inventory()))
    services=Application(settings).services
    services.write('approve-event', 'mailbox_select', {
        'event_id':event_id,'expected_revision':1,'expected_queue_revision':0})
    promoted = services.write('promote-event', 'promote_event', {
        'event_id': event_id, 'expected_revision': 1,
        'expected_queue_revision':1,
        'title': '我记得的雨声', 'body_md': '那天的雨声还在。'})
    assert Scenes(settings.database).evidence(promoted['id'])['evidence_status'] == 'bound'
    with narrative_transaction(settings.database) as rolls:
        assert RevisionInbox(rolls.store).list()['count'] == 0
    scout = Scout(settings)
    inventory = asyncio.run(scout._active_narrative_material_inventory())
    assert not any(item['source_type'] == 'event' and item['source_id'] == event_id for item in inventory)
    assert any(item['source_type'] == 'scene' and item['source_id'] == promoted['id'] for item in inventory)
    narrative = asyncio.run(scout.read_narrative('narrative_test'))
    freshness = asyncio.run(scout._narrative_material_freshness(narrative, ZoneInfo('Asia/Shanghai')))
    assert not any(item['source_type'] == 'event' and item['source_id'] == event_id for item in freshness)
    report = asyncio.run(scout._scan_narrative_revision_inbox(include_external=False))
    assert report['checked_rolls'] == 1


def test_auto_arc_routes_materials_without_changing_authored_body(live):
    settings, client = live
    seed_narrative(client, settings)
    diary_existing = client.post('/diaries', json={'content':'今天仍记得窗边的雨。','date':'2026-09-18','author':'user','title':'雨夜日记'}).json()['id']
    diary_new = client.post('/diaries', json={'content':'纸箱终于拆完了。','date':'2026-09-18','author':'user','title':'搬家日记'}).json()['id']
    with Store(settings.database) as store, store.transaction():
        store.create('scene_later', 'scene', '后来的一场雨', '后来我们又一起听雨。',
                     metadata={'object_kind':'scene','date':'2026-09-18'})
        store.create('scene_new_arc', 'scene', '新住处', '我们开始整理新的房间。',
                     metadata={'object_kind':'scene','date':'2026-09-18'})
    before = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    changes = Scout(settings).apply_arc_candidates([
        {'target_narrative_id':'narrative_test','title':'雨声','reason':'延续同一条雨声叙事',
         'source_event_ids':[],'source_scene_ids':['scene_later'],'source_diary_ids':[str(diary_existing)]},
        {'target_narrative_id':'','title':'新房间','reason':'形成新的生活线',
         'source_event_ids':[],'source_scene_ids':['scene_new_arc'],'source_diary_ids':[str(diary_new)]},
    ], model='synthetic')
    assert [item['type'] for item in changes] == ['existing_arc_materials','new_collecting_arc']
    after = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    assert after['body'] == before['body'] and after['published_at'] == before['published_at']
    assert after['linked_scene_ids'][-1] == 'scene_later' and after['linked_diary_ids'][-1] == diary_existing
    created = client.get('/api/narrative-rolls?narrative_id=' + changes[1]['narrative_id']).json()
    assert created['publication_status'] == 'collecting' and created['body'] == ''
    assert created['linked_scene_ids'] == ['scene_new_arc'] and created['linked_diary_ids'] == [diary_new]
