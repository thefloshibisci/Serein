"""Synthetic names and separated topics; never private memory fixtures."""
import pytest
from serein.config import Settings
from serein.core.store import Store
from serein.recall.index import Search, build_index
from serein.recall.service import Recall
from serein.api.read_text import memory_text, recall_text


@pytest.fixture
def lookup(tmp_path):
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db')
    with Store(settings.database) as store:
        store.create('a','scene','Book note','Bluefin visited the garden.')
        store.create('b','scene','Travel note','Wenlin visited the library.')
        store.create('hidden','scene','Hidden','Bluefin Wenlin',manual_surface=False)
        store.create('locked','scene','Locked','Bluefin Wenlin',metadata={'locked':True})
        store.create('stale','scene','Stale','Bluefin Wenlin')
    build_index(settings.database,settings.index)
    with Store(settings.database) as store:
        store.revise('stale',expected_revision=1,title='Changed',body_md='Unrelated.')
    return settings


def test_manual_multikeyword_fallback_preserves_visibility_and_labels_partial(lookup):
    result=Recall(lookup).run('Bluefin Wenlin',mode='lookup',surface_only=True)
    assert set(result['selected_refs'])=={'scene:a','scene:b'}
    hits=result['pools']['scene']['items']
    assert all(h['match_mode']=='keyword_fallback' and len(h['matched_keywords'])==1 for h in hits)
    text=recall_text(result,with_evidence=False)
    assert 'keyword_fallback' in text and 'matched_keywords:' in text
    assert not result['injected']


def test_strict_match_remains_first_and_automatic_search_does_not_broaden(lookup):
    with Search(lookup.database,lookup.index) as search:
        assert not search.search('Bluefin Wenlin',mode='surface')['items']
    with Store(lookup.database) as store:
        store.create('both','scene','Shared visit','Bluefin and Wenlin went to the garden.')
    lookup.index.unlink();build_index(lookup.database,lookup.index)
    result=Recall(lookup).run('Bluefin Wenlin',mode='lookup',surface_only=True)
    assert result['selected_refs']==['scene:both']
    assert result['pools']['scene'].get('match_mode')!='keyword_fallback'


def test_multiple_chinese_keywords_and_limit(tmp_path):
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db')
    with Store(settings.database) as store:
        store.create('a','scene','花园来信','小鲸在花园看书。')
        store.create('b','scene','图书馆来信','林小雨去图书馆。')
    build_index(settings.database,settings.index)
    result=Recall(settings).run('小鲸 林小雨',mode='lookup')
    assert set(result['selected_refs'])=={'scene:a','scene:b'}
    assert len(Recall(settings).run('小鲸 林小雨',mode='lookup',limit=1)['selected_refs'])==1


def test_partial_coverage_ranks_across_kinds_and_keeps_candidate_scope(lookup):
    with Store(lookup.database) as store:
        store.create('event_single','event','An event','Bluefin left a note.')
        store.create('scene_pair','scene','Two visitors','Bluefin and Wenlin met.')
    lookup.index.unlink();build_index(lookup.database,lookup.index)
    result=Recall(lookup).run('Bluefin Wenlin missing',mode='lookup',surface_only=True,limit=1)
    assert result['selected_refs']==['scene:scene_pair']
    with Search(lookup.database,lookup.index) as search:
        scoped=search.search('Bluefin Wenlin',mode='surface',keyword_fallback=True,candidate_ids={'a'})
        assert [h['id'] for h in scoped['items']]==['a']


def test_default_read_counts_sources_without_expanding_directory():
    evidence=[{'source_id':f'source_{i}','content':f'Original {i}',
               'metadata':{'source_system':'synthetic','session_id':'session','message_id':str(i)}} for i in range(35)]
    result={'id':'memory','kind':'event','readable':True,'document':{'title':'Visit','body_md':'Memory body','revision':3,'metadata':{},'created_at':'2025-01-01'},'evidence':evidence}
    short=memory_text(result,with_evidence=False)
    assert 'body:\nMemory body' in short and 'revision: 3' in short and 'bound_sources: 35' in short
    assert '[source ' not in short and 'source_id=' not in short and 'Original 0' not in short
    assert 'with_evidence=true' in short
    full=memory_text(result,with_evidence=True)
    assert full.count('[source ')==35 and 'Original 34' in full
