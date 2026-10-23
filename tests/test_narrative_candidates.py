import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest

from serein.compat import narrative_candidates as candidates
from serein.compat.germany.narrative_revision_scout import build_keyword_corridors, build_new_roll_candidate_prompt
from serein.compat.scout import Scout
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import Search, build_index
from serein.tagging_entities import snapshot, validate
from test_public_features import settings as idle_settings


def material(key, text='', *, kind='scene', names=(), bound=False):
    return {'source_type': kind, 'source_id': key, 'title': '', 'summary': text,
            'search_text': text, 'entity_names': list(names),
            'bound_narrative_ids': ['old-arc'] if bound else []}


def tag(store, key, name):
    doc = store.read(key)
    sources, stamp = snapshot(store, doc)
    entities, _ = validate([{'name': name, 'type': 'project',
                             'supports': [{'source_id': sources[0]['source_id'], 'quote': sources[0]['text']}],
                             'aliases': ['unverified-alias']}], sources)
    store.revise(key, expected_revision=doc['revision'], title=doc['title'], body_md=doc['body_md'],
                 metadata={**doc['metadata'], 'tagged_entities': entities,
                           'entity_extraction_version': 1, 'entity_input_hash': stamp})


def enable_scout(settings):
    save_settings(settings.database, {'models': [{'id': 'scout', 'model': 'synthetic-scout',
        'base_url': 'http://127.0.0.1:9/v1'}], 'assignments': {'narrative_scout': 'scout'},
        'features': {'narrative_nightly_organize': True}})


def test_current_entities_require_unchanged_body_and_bound_sources(tmp_path):
    settings = Settings(tmp_path / 'memory.db')
    with Store(settings.database) as store:
        store.create('current', 'scene', '第一次', '这里没有具体项目名。')
        source = store.add_source('source/1', 'Orion 完成了新的章节。')
        store.bind('current', source)
        tag(store, 'current', 'Orion')
    inventory = [material('current', '这里没有具体项目名。')]
    assert candidates.add_current_entities(settings, inventory)[0]['entity_names'] == ['Orion']
    with Store(settings.database) as store:
        doc = store.read('current')
        store.revise('current', expected_revision=doc['revision'], title='第二次', body_md='换了话题。')
    assert candidates.add_current_entities(settings, inventory)[0]['entity_names'] == []


def test_entity_route_recovers_names_outside_keyword_text_and_untagged_diary():
    seed = material('seed', '新阶段', names=['Orion'])
    old = material('old', '旧事情', kind='event', names=['Orion'])
    diary = material('7', '今天讨论 Orion 的下一章。', kind='diary')
    inventory = [seed, old, diary, material('prefix', 'OrionPlus 发布了。'),
                 material('bound', 'Orion', bound=True), material('alias', 'unverified-alias')]
    corridors = build_keyword_corridors(inventory, ['scene:seed'])
    assert corridors[0]['candidates'] == []
    found = candidates.entity_candidates(inventory, [seed])['scene:seed']
    assert {row['source_id'] for row in found} == {'old', '7'}
    # A diary seed with no tagging of its own can also use grounded vocabulary.
    reverse = candidates.entity_candidates(inventory, [diary])['diary:7']
    assert {row['source_id'] for row in reverse} == {'seed', 'old'}


def test_union_preserves_lexical_budget_and_tracks_duplicate_routes():
    seed = material('seed')
    lexical = [material(f'k{i}') for i in range(24)]
    corridor = {'seed': seed, 'candidates': lexical, 'keywords': []}
    entities = {'scene:seed': [dict(lexical[0], matched_entities=['Orion'])] +
                            [material(f'e{i}') for i in range(3)]}
    semantic = {'scene:seed': [dict(lexical[0], semantic_score=0.8)] +
                             [material(f'v{i}') for i in range(3)]}
    merged = candidates.merge_candidates([corridor], entities, semantic)
    rows = merged[0]['candidates']
    assert len(rows) == 30
    assert [row['source_id'] for row in rows[:24]] == [row['source_id'] for row in lexical]
    assert rows[0]['candidate_sources'] == ['keyword', 'entity', 'semantic']
    assert rows[0]['candidate_ranks'] == {'keyword': 1, 'entity': 1, 'semantic': 1}
    assert rows[0]['matched_entities'] == ['Orion'] and rows[0]['semantic_score'] == 0.8
    prompt = build_new_roll_candidate_prompt(merged, role_rules='rules')[1]['content']
    assert 'v2' in prompt and 'candidate_sources' in prompt


@pytest.mark.parametrize('failure', [ValueError('provider private details'), sqlite3.OperationalError('missing index')])
def test_semantic_failure_retains_keyword_and_entity_candidates(tmp_path, monkeypatch, failure):
    settings = Settings(tmp_path / 'db', tmp_path / 'index', embedding={'endpoint': 'https://example.test', 'api_key_env': 'TEST'})
    seed = material('seed', '共同关键词', names=['Orion'])
    old = material('old', '共同关键词')
    entity = material('entity', '完全不同', names=['Orion'])
    inventory = [seed, old, entity]
    corridor = {'seed': seed, 'candidates': [old], 'keywords': []}
    def fail(*args):
        raise failure
    monkeypatch.setattr(candidates, '_semantic_query', fail)
    result, receipt = asyncio.run(candidates.supplement_candidates(settings, inventory, [corridor]))
    assert {row['source_id'] for row in result[0]['candidates']} == {'old', 'entity'}
    assert receipt['semantic'] == {'status': 'failed', 'failed_queries': 1}
    assert 'private' not in str(receipt)


def test_semantic_results_are_filtered_before_limit_and_reject_stale_index(tmp_path, monkeypatch):
    settings = Settings(tmp_path / 'db', tmp_path / 'index')
    with Store(settings.database) as store:
        for key in ('seed', 'bound', 'archived', 'target', 'stale'):
            store.create(key, 'scene', key, '不重合的内容 ' + key)
    build_index(settings.database, settings.index)
    profile = {'model': 'test', 'provider_host': 'test', 'document_instruction': '',
               'query_instruction': '', 'max_chars': 4000}
    with sqlite3.connect(settings.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        conn.executemany('INSERT INTO vectors VALUES (?,?,?)', [(key, '[1,0]', 2) for key in
                                                            ('seed', 'bound', 'archived', 'target', 'stale')])
    with Store(settings.database) as store:
        store.revise('stale', expected_revision=1, title='stale', body_md='已修改')
    with Search(settings.database, settings.index) as search:
        result = search.search('query', mode='lookup', limit=1,
                               query_embedding={'query': 'query', 'profile': profile, 'embedding': [1, 0]},
                               min_cosine=0.3, candidate_ids={'stale', 'target'})
        assert [row['id'] for row in result['items']] == ['target']
        assert result['suppressed']['stale_content_rebuild_required'] == 1
        assert search.search('query', mode='lookup', limit=1,
                             query_embedding={'query': 'query', 'profile': profile, 'embedding': [1, 0]},
                             min_cosine=0.3, candidate_ids=set())['items'] == []
    class Embed:
        def __init__(self, *args, **kwargs):
            pass
        def query(self, text):
            return {'query': text, 'profile': profile, 'embedding': [1, 0]}
    monkeypatch.setattr(candidates, 'EmbeddingClient', Embed)
    inventory = [material('seed', '新经历'), material('bound', '相似但已归卷', bound=True), material('target', '另一种说法')]
    result, status = asyncio.run(candidates.semantic_candidates(settings, inventory, inventory[:1]))
    assert status['status'] == 'unconfigured'
    settings = replace(settings, embedding={'endpoint': 'https://example.test', 'api_key_env': 'TEST'})
    result, status = asyncio.run(candidates.semantic_candidates(settings, inventory, inventory[:1]))
    assert status['status'] == 'ok'
    assert [row['source_id'] for row in result['scene:seed']] == ['target']


def test_daily_scan_supplies_entity_only_pair_and_notices_new_tags(idle_settings, tmp_path, monkeypatch):
    settings = idle_settings
    with Store(settings.database) as store:
        for key, body in [('first', '春天到了'), ('second', '修好了水管')]:
            store.create(key, 'scene', key, body, metadata={'object_kind': 'scene'})
            store.bind(key, store.add_source('src/' + key, 'Orion 完成了新章节。'))
    config = tmp_path / 'scout.yaml'
    config.write_text('narrative_rolls:\n  new_roll_scout_enabled: true\n', encoding='utf-8')
    settings = replace(settings, background={'germany_config_file': str(config)})
    enable_scout(settings)
    captured = []
    async def propose(**kwargs):
        captured.append(kwargs['corridors'])
        return []
    monkeypatch.setattr('serein.compat.germany.narrative_scan.propose_new_roll_candidates', propose)
    monkeypatch.setattr(Scout, 'role_rules', lambda self: 'rules')
    scout = Scout(settings)
    first = asyncio.run(scout._scan_narrative_revision_inbox())
    assert first['external_scout_status'] == 'ok'
    assert all(not row['candidates'] for row in captured[0])
    with Store(settings.database) as store:
        tag(store, 'first', 'Orion')
        tag(store, 'second', 'Orion')
    second = asyncio.run(scout._scan_narrative_revision_inbox())
    assert second['external_scout_status'] == 'ok' and len(captured) == 2
    assert all(row['candidates'][0]['candidate_sources'] == ['entity'] for row in captured[1])
    assert first['external_input_sha256'] != second['external_input_sha256']
    assert asyncio.run(scout._scan_narrative_revision_inbox())['external_scout_status'] == 'unchanged'
    assert second['existing_arcs_updated'] == second['new_collecting_arcs_created'] == 0
    # The rollback flag restores the original lexical candidate pool.
    config.write_text('narrative_rolls:\n  new_roll_scout_enabled: true\n  hybrid_candidates_enabled: false\n', encoding='utf-8')
    rolled_back = asyncio.run(scout._scan_narrative_revision_inbox())
    assert rolled_back['external_scout_status'] == 'ok'
    assert all(not row['candidates'] for row in captured[-1])


def test_failed_semantic_lane_retries_after_recovery_without_losing_lexical(idle_settings, tmp_path, monkeypatch):
    settings = idle_settings
    with Store(settings.database) as store:
        for key in ('first', 'second'):
            store.create(key, 'scene', key, 'Orion 发布新版本。', metadata={'object_kind': 'scene'})
    config = tmp_path / 'scout.yaml'
    config.write_text('narrative_rolls:\n  new_roll_scout_enabled: true\n', encoding='utf-8')
    settings = replace(settings, background={'germany_config_file': str(config)},
                       embedding={'endpoint': 'https://example.test', 'api_key_env': 'TEST'})
    enable_scout(settings)
    monkeypatch.setattr(Scout, 'role_rules', lambda self: 'rules')
    def fail(*args):
        raise ValueError('unavailable')
    monkeypatch.setattr(candidates, '_semantic_query', fail)
    calls = []
    async def propose(**kwargs):
        calls.append(kwargs['corridors'])
        assert all(row['candidates'] for row in kwargs['corridors'])
        return []
    monkeypatch.setattr('serein.compat.germany.narrative_scan.propose_new_roll_candidates', propose)
    scout = Scout(settings)
    failed = asyncio.run(scout._scan_narrative_revision_inbox())
    assert failed['external_scout_status'] == 'ok'
    assert failed['candidate_search']['semantic']['status'] == 'failed'
    assert failed['external_input_sha256'] == ''
    monkeypatch.setattr(candidates, '_semantic_query', lambda *args: [])
    recovered = asyncio.run(scout._scan_narrative_revision_inbox())
    assert recovered['candidate_search']['semantic']['status'] == 'ok'
    assert recovered['external_input_sha256'] and len(calls) == 2
    assert asyncio.run(scout._scan_narrative_revision_inbox())['external_scout_status'] == 'unchanged'


def test_public_semantic_lane_uses_prepared_selected_model_and_fails_soft(tmp_path, monkeypatch):
    original = Settings(tmp_path / 'db')
    selected = replace(original, index=tmp_path / 'selected-index',
                       embedding={'endpoint': 'http://127.0.0.1:9/embeddings', 'api_key': ''})
    seed, other = material('seed', 'new'), material('other', 'old')
    monkeypatch.setattr(candidates, 'effective_settings', lambda settings: selected)
    seen = []
    def query(settings, seed, eligible):
        seen.append(settings)
        return [other]
    monkeypatch.setattr(candidates, '_semantic_query', query)
    result, status = asyncio.run(candidates.semantic_candidates(original, [seed, other], [seed]))
    assert seen == [selected] and status['status'] == 'ok'
    assert result['scene:seed'] == [other]
    def unprepared(settings):
        raise ValueError('selected model needs preparation')
    monkeypatch.setattr(candidates, 'effective_settings', unprepared)
    result, status = asyncio.run(candidates.semantic_candidates(original, [seed, other], [seed]))
    assert result == {} and status == {'status': 'failed', 'failed_queries': 1}
    assert seen == [selected]
