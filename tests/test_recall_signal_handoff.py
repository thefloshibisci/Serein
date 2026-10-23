"""Exercise the real signal collector and mixed-pool handoff with local indexes."""
from types import SimpleNamespace

import pytest

from serein.recall import typed_surface
from serein.recall.germany.candidates import CandidateGateway


def memory(kind, owner_id, score):
    return {'owner_kind':kind, 'owner_id':owner_id, 'score':score,
            'title':owner_id, 'body':'synthetic repair evidence',
            'memory_date':'2026-09-01', 'recallable':True}


def collect_and_select(memories, *, cues=(), keywords=(), limit=20):
    snapshot=SimpleNamespace(
        catalog={row['owner_id']:row for row in memories},
        whole={row['owner_id']:row['score'] for row in memories},
        passages={}, cutoff=-1.0)
    gateway=CandidateGateway.__new__(CandidateGateway)
    gateway.passage_candidate_shadow_enabled=True
    gateway._passage_candidate_shadow_catalog=snapshot.catalog
    gateway.recall_policy=SimpleNamespace(specific_query_terms=lambda query:['repair'])
    gateway._matched_query_term_is_specific=lambda term:True
    gateway.passage_shadow_index=SimpleNamespace(search_by_embedding=lambda *args,**kwargs:{'matches':[]})
    gateway.embedding_engine=SimpleNamespace(search_scene_whole_by_embedding=lambda *args,**kwargs:[
        {'scene_id':row['owner_id'],'score':row['score']} for row in memories
        if row['owner_kind']=='scene'][:kwargs['top_k']])
    gateway.fact_event_semantic_index=SimpleNamespace(search_by_embedding=lambda *args,**kwargs:{'matches':[
        {'memory_id':row['owner_id'],'memory_kind':'event','score':row['score']} for row in memories
        if row['owner_kind']=='event'][:kwargs['top_k']]})
    gateway.cue_passage_shadow_index=SimpleNamespace(search_by_embedding=lambda *args,**kwargs:{'matches':list(cues)[:kwargs['top_k']]})
    gateway.fact_event_lexical_shadow_index=SimpleNamespace(search=lambda *args,**kwargs:{'matches':list(keywords)[:kwargs['top_k']]})
    query=SimpleNamespace(text='phone repair')
    signals=gateway.collect_signals(query.text,[1.0,0.0])
    assert signals['status']=='ok'
    ranked=typed_surface.mixed_candidates(snapshot,signals,{},query)
    return typed_surface.select_candidate_pool(snapshot,signals,ranked,[],query,limit=limit)


def base_memories():
    return [memory('scene' if index%2==0 else 'event',f'base{index}',.9-index*.02)
            for index in range(6)]


def cue(owner_id, score=.71):
    return {'owner_kind':'scene','owner_id':owner_id,'score':score,'matched_cues':['repair screen']}


def keyword(owner_id, *, specific_terms=('repair',)):
    return {'owner_kind':'event','owner_id':owner_id,'score':2.4,'specific_terms':list(specific_terms)}


def test_collected_cue_and_keyword_admit_weak_tail_without_changing_vector_scores():
    memories=base_memories()+[memory('scene','cue-tail',.42),memory('event','keyword-tail',.41)]
    rows,counts=collect_and_select(memories,cues=[cue('cue-tail')],keywords=[keyword('keyword-tail')])
    by_id={row['owner_id']:row for row in rows}
    assert set(by_id)=={row['owner_id'] for row in memories}
    assert by_id['cue-tail']['entry_reasons']==['semantic_cue']
    assert by_id['cue-tail']['score']==.42
    assert by_id['cue-tail']['signal_scores']['cue_cosine']==.71
    assert by_id['keyword-tail']['entry_reasons']==['event_specific_keyword']
    assert by_id['keyword-tail']['score']==.41
    assert by_id['keyword-tail']['specific_terms']==['repair']
    assert counts['base_pool']==6
    assert counts['cue_expansion']==counts['keyword_expansion']==1


def test_collected_signals_do_not_duplicate_base_or_tail_owners():
    memories=base_memories()+[memory('scene','cue-tail',.42),memory('event','keyword-tail',.41)]
    rows,counts=collect_and_select(memories,
        cues=[cue('base0'),cue('base0'),cue('cue-tail'),cue('cue-tail')],
        keywords=[keyword('base1'),keyword('base1'),keyword('keyword-tail'),keyword('keyword-tail')])
    keys=[(row['owner_kind'],row['owner_id']) for row in rows]
    assert len(keys)==len(set(keys))==8
    assert rows[0]['entry_reasons']==['base_vector_rank']
    assert counts['base_pool']==6
    assert counts['cue_expansion']==counts['keyword_expansion']==1


@pytest.mark.parametrize('limit,cue_count,keyword_count',[(8,2,0),(10,3,1),(20,3,3)])
def test_collected_signals_preserve_base_expansion_and_total_budgets(limit,cue_count,keyword_count):
    cue_memories=[memory('scene',f'cue{index}',.49-index*.005) for index in range(5)]
    keyword_memories=[memory('event',f'keyword{index}',.46-index*.005) for index in range(5)]
    memories=base_memories()+cue_memories+keyword_memories+[memory('scene','weak',.40)]
    rows,counts=collect_and_select(memories,cues=[cue(row['owner_id']) for row in cue_memories],
        keywords=[keyword(row['owner_id']) for row in keyword_memories],limit=limit)
    assert [row['owner_id'] for row in rows[:6]]==[f'base{index}' for index in range(6)]
    assert len(rows)==6+cue_count+keyword_count<=limit
    assert counts['base_pool']==6
    assert counts['cue_expansion']==cue_count
    assert counts['keyword_expansion']==keyword_count
    assert 'weak' not in {row['owner_id'] for row in rows}


def test_collected_weak_cue_and_missing_keyword_do_not_bypass_tail_gate():
    memories=base_memories()+[memory('scene','weak-cue',.42),memory('event','no-keyword',.41)]
    rows,counts=collect_and_select(memories,cues=[cue('weak-cue',.5499)])
    assert [row['owner_id'] for row in rows]==[f'base{index}' for index in range(6)]
    assert counts['cue_expansion']==counts['keyword_expansion']==0
