from serein.compat.germany.narrative_revision_scout import normalize_new_roll_candidates


def test_auto_arc_scout_routes_diary_to_existing_arc_and_rejects_invented_target():
    corridors = [{'seed': {'source_type':'diary','source_id':'7','title':'雨夜',
                           'bound_narrative_ids':[]}, 'candidates': []}]
    rolls = [{'narrative_id':'narrative_rain','title':'雨声','query_cues':['雨夜']}]
    output = {'candidates':[{'seed_source_type':'diary','seed_source_id':'7',
        'target_narrative_id':'narrative_rain','title':'雨声','reason':'延续雨夜生活线',
        'confidence':'high','materials':[{'source_type':'diary','source_id':'7'}]}]}
    routed = normalize_new_roll_candidates(output, corridors, existing_rolls=rolls)
    assert routed[0]['target_narrative_id'] == 'narrative_rain'
    assert routed[0]['source_diary_ids'] == ['7']
    output['candidates'][0]['target_narrative_id'] = 'invented'
    assert normalize_new_roll_candidates(output, corridors, existing_rolls=rolls) == []


def test_auto_arc_scout_requires_two_materials_for_new_collecting_arc():
    corridors = [{'seed': {'source_type':'diary','source_id':'7','title':'搬家日记',
                           'bound_narrative_ids':[]},
                  'candidates':[{'source_type':'scene','source_id':'scene_room','title':'新房间',
                                 'bound_narrative_ids':[]}]}]
    item = {'seed_source_type':'diary','seed_source_id':'7','target_narrative_id':'',
            'title':'新房间','reason':'两份材料形成新的生活线','confidence':'high',
            'materials':[{'source_type':'diary','source_id':'7'},
                         {'source_type':'scene','source_id':'scene_room'}]}
    routed = normalize_new_roll_candidates({'candidates':[item]}, corridors, existing_rolls=[])
    assert routed[0]['source_diary_ids'] == ['7'] and routed[0]['source_scene_ids'] == ['scene_room']
    item['materials'] = item['materials'][:1]
    assert normalize_new_roll_candidates({'candidates':[item]}, corridors, existing_rolls=[]) == []


import hashlib
from types import SimpleNamespace
import pytest
from serein.compat.germany.narrative_scan import NarrativeScout


@pytest.mark.parametrize("content", ["  original text  ", "\n原文保持换行\n", "\t原文\t"])
def test_scout_verifies_exact_source_before_formatting(content):
    event={'status':'active','fingerprint':'synthetic','source_refs':[{
        'content':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()}]}
    scout=NarrativeScout()
    scout.events=SimpleNamespace(read=lambda *a,**k:event)
    result=scout._hydrate_event_scout_material({'source_type':'event','source_id':'synthetic'})
    assert result['source_excerpt']==content.strip()
    assert event['source_refs'][0]['content']==content


def test_scout_still_rejects_modified_source():
    event={'status':'active','source_refs':[{'content':'changed',
        'content_sha256':hashlib.sha256(b'original').hexdigest()}]}
    scout=NarrativeScout()
    scout.events=SimpleNamespace(read=lambda *a,**k:event)
    with pytest.raises(RuntimeError,match='event_source_drift'):
        scout._hydrate_event_scout_material({'source_type':'event','source_id':'synthetic'})
