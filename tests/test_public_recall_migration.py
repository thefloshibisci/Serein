import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from test_public_settings import deployment, configure
from test_surface_selection import legacy
from test_recall_policy import setup
from test_live_events import item
from serein.configured_models import effective_settings, memory_ready
from serein.core import Store
from serein.core.reader import Reader
from serein.compat.narratives import narrative_transaction
from serein.recall.service import Recall
from serein.recall.scene import related_candidates
from serein.recall.query import Query
from serein.recall.surface_gate import load_gate
from serein.recall.worker import update_pending


@pytest.mark.parametrize('metadata,excluded', [
    ({'domain':['tech']},True), ({'domain':'tech'},True),
    ({'domain':['', 'tech', 'life']},True),
    ({'canonical_domain':'tech','domain':['life']},True),
    ({'canonical_domain':'life','domain':['tech']},False),
    ({'domain':['life','tech']},False),
])
def test_legacy_domain_exclusions_apply_to_lookup_and_both_edge_ends(setup,metadata,excluded):
    from serein.deployment import save_settings,DEFAULT_DOMAINS
    save_settings(setup.database,{'features':{'association':True},'tagging':{'domains':[
        {**row,'policy':setup.recall['domains'].get(row['key'],row['policy'])} for row in DEFAULT_DOMAINS]}})
    with Store(setup.database) as store:
        old=store.read('scene_tech')
        store.revise('scene_tech',expected_revision=old['revision'],title=old['title'],body_md=old['body_md'],metadata=metadata)
        # The shared fixture's earlier edge otherwise consumes the single
        # neighbor slot before this test can exercise the target domain.
        store.conn.execute("DELETE FROM scene_relations WHERE id='edge'")
        store.conn.execute("INSERT INTO scene_relations VALUES ('tech-edge','test','scene_tech','scene_a','active',1,'{}')")
    from serein.recall.index import refresh_index
    refresh_index(setup.database,setup.index,{'scene_tech'})
    engine=Recall(setup)
    assert bool(engine.run('技术说明',mode='lookup')['selected_refs']) is not excluded
    with Reader(setup.database) as reader:
        assert bool(related_candidates(reader,['scene_tech'],Query('技术说明'),engine.policy)) is not excluded
        targets=related_candidates(reader,['scene_a'],Query('世界之窗'),engine.policy)
        assert len(targets)<=1
        assert any(row['id']=='scene_tech' for row in targets) is not excluded


def test_excluded_legacy_domain_never_reaches_mixed_reranker(legacy):
    settings,build=legacy
    _,calls=build([('tech','scene',.99,'2026-09-01'),('allowed','event',.8,'2026-09-01')])
    with Store(settings.database) as store:
        old=store.read('tech')
        store.revise('tech',expected_revision=1,title=old['title'],body_md=old['body_md'],metadata={'domain':['tech'],'date':'2026-09-01'})
    from serein.recall.index import refresh_index
    refresh_index(settings.database,settings.index,{'tech'})
    with sqlite3.connect(settings.index) as conn:
        conn.execute("INSERT OR REPLACE INTO vectors VALUES ('tech','[1,0]',2)")
    def score(query,documents):
        calls.extend(documents)
        return {row['ref']:.9 for row in documents}
    result=Recall(replace(settings,recall={**settings.recall,'domains':{'tech':'excluded'}}),reranker=score).run('手机维修',method='semantic',min_cosine=.5)
    assert result['selected_refs']==['event:allowed']
    assert all(row['ref']!='scene:tech' for row in calls)


def test_domain_projection_and_settings_policy_agree_without_rewriting(deployment):
    settings,client=deployment
    with Store(settings.database) as store:
        store.create('old','scene','旧格式场景','合成测试正文',metadata={'domain':['tech']})
        store.create('new','scene','新格式场景','合成测试正文',metadata={'domain':['tech'],'canonical_domain':'life'})
    result=client.post('/api/serein/memory-projection',json={}).json()['scenes']
    assert {row['source_id']:row['bucket_domain'] for row in result}=={'old':'tech','new':'life'}
    light=client.get('/api/buckets/light').json()['buckets']
    assert {row['id']:row['canonical_domain'] for row in light}=={'old':'tech','new':'life'}
    with Store(settings.database,read_only=True) as store:
        assert store.read('old')['metadata']=={'domain':['tech']}


def test_settings_prepare_wires_full_routes_entities_and_arc_then_refreshes_evidence(deployment,monkeypatch):
    settings,client=deployment
    configure(client)
    client.patch('/v1/settings',json={'identity':{'user_name':'Nori','ai_name':'Atlas'},
        'assignments':{'embedding':'model-a','reranker':'model-a'}}).raise_for_status()
    refs=item()['source_refs'];refs[0]['content']='今天读了《星港》，讨论故事里的飞船。'
    result=client.post('/v1/tools/call',json={'name':'write_scene','arguments':{
        'title':'读书记录','content':'读完了开篇。','cues':['读书'],'evidence_refs':refs}})
    scene=result.json()['result'].split('[scene_id:')[1].split(']')[0]
    with Store(settings.database) as store:
        store.create('second','scene','另一次阅读','继续读后面的章节。')
    with narrative_transaction(settings.database,write=True) as rolls:
        result=rolls.publish(narrative_id='narrative_star',document=f'# 星港\n\n## 第一人称叙事\n\n这次阅读留下了印象。\n\n## 材料\n\n{scene}\nsecond\n',
            expected_revision=0,title='星港',arc_key='arc:star',source_scene_ids=[scene,'second'])
        assert result['status']=='created',result
    original=httpx.Client;inputs=[]
    def handle(request):
        assert request.url.host=='127.0.0.1'
        body=json.loads(request.content);texts=body['input'];texts=texts if isinstance(texts,list) else [texts]
        inputs.extend(texts)
        return httpx.Response(200,json={'model':'synthetic-model','data':[
            {'index':i,'embedding':[1.,0.,0.,0.]} for i in range(len(texts))]})
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    response=client.post('/v1/settings/prepare-memory')
    assert response.status_code==200,response.text
    assert response.json()['routes']==5 and response.json()['scope_entities']>0
    selected=effective_settings(settings)
    assert not settings.background and memory_ready(settings)
    routes=json.loads(Path(selected.recall['routing_file']).read_text('utf-8'))
    assert len(routes['routes'])==5 and sum(len(r['vectors']) for r in routes['routes'])==67
    assert len(routes['boundaries'])==11 and routes['generation']=='public-synthetic-v2'
    assert 'Atlas，下午好呀。' in inputs
    assert all('{ai_name}' not in text and '{user_name}' not in text for text in inputs)
    assert routes['policy']==dict(min_score=.72,min_margin=.06,aggregation_top_k=1,
        boundary_veto_enabled=True,boundary_veto_min_score=.62,boundary_veto_max_deficit=.03)
    gate=load_gate(selected.recall['germany_policy_file'],'Nori','Atlas')
    assert 'nori' in gate.engine.recall_policy.options.user_terms
    assert 'atlas' in gate.engine.recall_policy.options.context_terms
    assert set(gate.engine.recall_policy.options.context_terms) == {
        'user','assistant','用户','对方','我','你','她','他','ta','宝宝','老婆','亲爱的','nori','atlas'}
    index=gate.engine.observed_entity_shadow_index
    assert index.resolve_query('星港最近的进展是什么')['scope_anchor']['arc_key']=='arc:star'
    assert any(row['owner_id']==scene for row in index.owner_query_matches('星港',owner_keys=[('scene',scene)]))
    # The scope is a candidate boundary; an Arc's authored body is never injected here.
    from serein.recall.typed_surface import scope_members
    with Reader(settings.database) as reader:
        assert scope_members(reader)['arc:star']=={('scene',scene),('scene','second')}
    evidence=client.post('/v1/tools/call',json={'name':'read_scene_evidence','arguments':{'scene_id':scene}}).json()['result']
    client.post('/v1/tools/call',json={'name':'unbind_scene_evidence','arguments':{
        'scene_id':scene,'evidence_ids':[evidence['evidence_refs'][0]['id']]}}).raise_for_status()
    update_pending(selected)
    assert not index.owner_query_matches('星港',owner_keys=[('scene',scene)])
    # An Arc-only revision also queues a refresh, without a private job config.
    with narrative_transaction(settings.database,write=True) as rolls:
        changed=rolls.publish(narrative_id='narrative_star',
            document=f'# 深海\n\n## 第一人称叙事\n\n换了新的卷名。\n\n## 材料\n\n{scene}\nsecond\n',
            expected_revision=1,title='深海',arc_key='arc:star',source_scene_ids=[scene,'second'])
        assert changed['status']=='updated',changed
    assert update_pending(selected)['updated']==1
    assert index.resolve_query('深海最近的进展')['scope_anchor']['arc_key']=='arc:star'
    ready=selected.index.parent/'ready.json'
    payload=json.loads(ready.read_text('utf-8'));payload['recall_resources']='old-six-example-version'
    ready.write_text(json.dumps(payload),'utf-8')
    assert not memory_ready(settings)
