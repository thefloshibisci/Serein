from types import SimpleNamespace

from serein.recall import typed_surface


def candidate(kind, owner_id, score):
    return {
        'owner_kind':kind,'owner_id':owner_id,'title':owner_id,'memory_date':'2026-09-01',
        'score':score,'passages':[{'text':owner_id,'score':score}],
        'score_components':{'whole':score},'candidate_sources':[f'{kind}_whole_embedding'],
        'signal_scores':{'whole_cosine':score},
    }


def found(*, cues=(), keywords=(), intent='none'):
    return {
        'entity_scope':{'intent':intent},
        'lanes':{
            'scene':{'cue_search':{'matches':list(cues)}},
            'event':{'lexical_search':{'matches':list(keywords)}},
        },
    }


def test_weak_vector_tail_does_not_fill_direct_pool():
    ranked=[candidate('scene',f's{i}',.9-i*.02) for i in range(6)]
    ranked += [candidate('scene',f'weak{i}',.49-i*.01) for i in range(8)]
    rows,counts=typed_surface.select_candidate_pool(None,found(),ranked,[],SimpleNamespace(text='手机维修'))
    assert [row['owner_id'] for row in rows]==[f's{i}' for i in range(6)]
    assert counts['base_pool']==6 and counts['tail_rejected']==8


def test_cue_keyword_and_full_entity_expand_only_reranker_entry():
    ranked=[candidate('scene',f's{i}',.9-i*.02) for i in range(6)]
    ranked += [candidate('scene','cue',.42),candidate('event','keyword',.41),candidate('scene','entity',.40)]
    channels=found(
        cues=[{'owner_id':'cue','score':.71,'matched_cues':['一起修屏幕']}],
        keywords=[{'owner_id':'keyword','score':2.4,'specific_terms':['维修店']}],
        intent='recall_reference')
    matches=[{'owner_kind':'scene','owner_id':'entity','entity':'皮卡堂'}]
    rows,counts=typed_surface.select_candidate_pool(
        None,channels,ranked,matches,SimpleNamespace(text='还记得皮卡堂吗'))
    by_id={row['owner_id']:row for row in rows}
    assert set(by_id).issuperset({'cue','keyword','entity'})
    assert by_id['cue']['score']==.42 and by_id['cue']['signal_scores']['cue_cosine']==.71
    assert by_id['keyword']['score']==.41 and by_id['keyword']['specific_terms']==['维修店']
    assert by_id['entity']['score']==.40 and by_id['entity']['entity_handles']==matches
    assert counts['cue_expansion']==counts['keyword_expansion']==counts['entity_expansion']==1


def test_weak_cue_top_k_is_not_an_entry_signal():
    ranked=[candidate('scene',f's{i}',.9-i*.02) for i in range(6)]
    ranked.append(candidate('scene','weak-cue',.40))
    channels=found(cues=[{'owner_id':'weak-cue','score':.5499,'matched_cues':['语义接近但不足']}])
    rows,counts=typed_surface.select_candidate_pool(None,channels,ranked,[],SimpleNamespace(text='手机维修'))
    assert 'weak-cue' not in {row['owner_id'] for row in rows}
    assert counts['cue_expansion']==0


def test_saved_candidate_thresholds_change_only_tail_eligibility():
    ranked=[candidate('scene',f's{i}',.40-i*.01) for i in range(6)]
    ranked += [candidate('event','body-tail',.48),candidate('scene','cue-tail',.30)]
    channels=found(cues=[{'owner_id':'cue-tail','score':.56,'matched_cues':['语义改写线索']}])
    strict,_=typed_surface.select_candidate_pool(None,channels,ranked,[],SimpleNamespace(text='手机维修'),
        body_threshold=.50,cue_threshold=.57)
    relaxed,_=typed_surface.select_candidate_pool(None,channels,ranked,[],SimpleNamespace(text='手机维修'),
        body_threshold=.47,cue_threshold=.55)
    assert {row['owner_id'] for row in strict}=={f's{i}' for i in range(6)}
    assert {row['owner_id'] for row in relaxed}.issuperset({'body-tail','cue-tail'})
    assert all(row['entry_reasons']==['base_vector_rank'] for row in strict)


def test_entity_name_requires_recall_intent():
    ranked=[candidate('scene',f's{i}',.9-i*.02) for i in range(6)]
    ranked.append(candidate('scene','entity',.40))
    matches=[{'owner_kind':'scene','owner_id':'entity','entity':'皮卡堂'}]
    rows,_=typed_surface.select_candidate_pool(None,found(),ranked,matches,SimpleNamespace(text='皮卡堂'))
    assert 'entity' not in {row['owner_id'] for row in rows}


def test_vector_cache_is_keyed_by_exact_content():
    typed_surface._VECTOR_CACHE.clear()
    first=typed_surface._decode_vector('[1.0,0.0]')
    repeated=typed_surface._decode_vector('[1.0,0.0]')
    changed=typed_surface._decode_vector('[0.0,1.0]')
    assert repeated is first
    assert changed is not first and list(changed)==[0.0,1.0]
