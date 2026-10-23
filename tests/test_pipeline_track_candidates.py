import json
from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_public_features import settings, ingest
from serein.api.settings import PipelinePatch
from serein.core.store import encode
from serein.deployment import read_settings, save_settings
from serein.extensions import pipeline as p
from serein.extensions.pipeline_track_candidates import select, tokens
from serein.extensions import pipeline_latest as latest


def card(key, text, time='2026-10-01T00:00:00Z', **extra):
    return {'track_id':key,'subject':text,'throughline':'进度待验证','status':'parked',
            'recent_turns':[{'text':text,'created_at':time}], **extra}


def test_direct_window_retrieval_dedup_and_complete_originals():
    cards=[card('recent','数据库','2026-10-03T23:00:00Z'),
           card('old','检修数据库'),card('other','布达佩斯电影')]
    original=deepcopy(cards)
    messages=[{'content':'哦哦哦 感觉 可以，数据库终于修好了','created_at':'2026-10-04T00:00:00Z'}]
    assert select(cards,messages,[],{})==cards
    result=select(cards,messages,[],{'track_candidates_enabled':True,'track_candidate_limit':1})
    assert [c['track_id'] for c in result]==['old','recent']
    assert cards==original and result[0]['recent_turns']==original[1]['recent_turns']
    assert not set(tokens('哦哦哦哦 感觉 可以'))
    assert '数据库' in tokens('数据库')


def test_whole_block_nearby_context_and_all_card_fields_are_searched():
    cards=[card('clue','记录',throughline='修理数据库'),
           card('original','笔记',recent_turns=[{'text':'明信片','created_at':'2026-10-01T00:00:00Z'}]),
           card('noise','不相关')]
    messages=[{'text':'昨天那个好了','created_at':'2026-10-04T00:00:00Z'},
              {'text':'就是数据库那个','created_at':'2026-10-04T00:01:00Z'}]
    result=select(cards,messages,[{'text':'明信片'}],{'track_candidates_enabled':True})
    assert {c['track_id'] for c in result}=={'clue','original'}


def test_direct_boundary_and_unknown_timestamps_do_not_drop_cards():
    cards=[card('boundary','无关联','2026-10-03T12:00:00Z'),
           card('outside','另件事','2026-10-03T11:59:59Z'),card('unknown','未知','invalid')]
    messages=[{'text':'哎','created_at':'2026-10-04T00:00:00Z'}]
    result=select(cards,messages,[],{'track_candidates_enabled':True})
    assert {c['track_id'] for c in result}=={'boundary','unknown'}
    assert select(cards,[{'text':'哎'}],[],{'track_candidates_enabled':True})==cards


@pytest.mark.parametrize('values',[{'track_candidate_limit':0},{'track_candidate_limit':True},
                                  {'track_direct_hours':13},{'track_candidates_enabled':'yes'}])
def test_invalid_settings_are_rejected(values):
    with pytest.raises(ValidationError):PipelinePatch(**values)


def test_request_uses_frozen_opt_in_policy_and_keeps_all_stored_cards(settings):
    assert read_settings(settings.database)['pipeline']['track_candidates_enabled'] is False
    save_settings(settings.database,{'pipeline':{'track_candidates_enabled':True,'track_candidate_limit':1}})
    ingest(settings);p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json'])
    # All unmatched older cards are still retained in the batch state.
    data['tracks']=[card('unmatched','不相关','2000-01-01T00:00:00Z')]
    batch={**batch,'input_json':encode(data)}
    request=p.request_for(settings.database,batch,'track_router')
    assert request['active_tracks']==[] and len(data['tracks'])==1
    save_settings(settings.database,{'pipeline':{'track_candidates_enabled':False}})
    assert p.request_for(settings.database,batch,'track_router')['active_tracks']==[]


def tag(prompt, name):
    return json.JSONDecoder().raw_decode(prompt.split(f'<{name}>',1)[1].lstrip())[0]


def test_compact_cards_only_omit_candidate_turns_without_mutating_sources():
    cards=[card('session_83_track_0026','Library labels',throughline='Inspect on Sunday',
                event_policy='rolling_engineering',task_status='in_progress')]
    messages=[{'id':3011,'session_id':83,'role':'user','content':'Sunday at two; leave the wiring.',
               'created_at':'2026-10-01T10:00:00Z'}]
    recent=[{**messages[0],'id':3001,'content':'Library B12 labels, originally Friday.'}]
    original=deepcopy((cards,messages,recent))
    old=latest.build_event_track_message_prompt('2026-10-01',messages,cards,recent_context_messages=recent)
    compact=latest.build_event_track_message_prompt('2026-10-01',messages,cards,recent_context_messages=recent,compact_cards=True)
    visible=tag(compact,'active_tracks_json')
    assert visible==[{key:value for key,value in tag(old,'active_tracks_json')[0].items() if key!='recent_turns'}]
    assert visible[0]['track_id']=='session_83_track_0026'
    for name in ('raw_messages_json','bounded_recent_context_json'):
        assert tag(old,name)==tag(compact,name)
    assert (cards,messages,recent)==original
    assert len(compact)<len(old)


@pytest.mark.parametrize('enabled',[False,True])
def test_compact_setting_uses_batch_policy_and_retains_complete_cards(settings,enabled):
    assert read_settings(settings.database)['pipeline']['router_compact_cards'] is False
    save_settings(settings.database,{'pipeline':{'router_compact_cards':enabled}})
    ingest(settings);p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json'])
    data['tracks']=[card('session_83_track_0026','Library labels')]
    batch={**batch,'input_json':encode(data)}
    # Changing the live setting does not reinterpret this batch's input policy.
    save_settings(settings.database,{'pipeline':{'router_compact_cards':not enabled}})
    request=p.request_for(settings.database,batch,'track_router')
    visible=tag(request['prompt'],'active_tracks_json')
    assert ('recent_turns' not in visible[0]) is enabled
    assert request['active_tracks'][0]['recent_turns']==data['tracks'][0]['recent_turns']
    assert tag(request['prompt'],'raw_messages_json')==latest.event_track_message_payload(request['messages'])
    assert tag(request['prompt'],'bounded_recent_context_json')==latest.event_track_message_payload(request['recent_context'])


@pytest.mark.parametrize('value',[0,1,'true',None])
def test_compact_setting_rejects_non_boolean_without_saving(settings,value):
    before=read_settings(settings.database)
    with pytest.raises(ValueError):
        save_settings(settings.database,{'pipeline':{'router_compact_cards':value}})
    assert read_settings(settings.database)==before
    if value is not None:
        with pytest.raises(ValidationError):PipelinePatch(router_compact_cards=value)
