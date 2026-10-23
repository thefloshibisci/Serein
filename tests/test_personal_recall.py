import json
import sqlite3

import pytest

from serein.config import Settings
from serein.core import Store
from serein.recall.germany.memory_recall.observed_entities import _query_intent
from serein.recall.index import build_index
from serein.recall.person_references import resolve_person_references
from serein.recall.service import Recall


IDENTITY = {'ai_name': 'Orion', 'user_name': 'Mira', 'user_display_name': '米拉'}


@pytest.mark.parametrize('marker', ['原话', '逐字', '哪天', '哪一天', '哪一次', '什么时候', '具体日期', '怎么说的'])
def test_evidence_words_no_longer_override_recall_intent(marker):
    assert _query_intent(f'还记得生日{marker}吗') == ('recall_reference', 'arc_index')


@pytest.mark.parametrize('query,expected', [
    ('你的生日是什么时候？', 'Orion的生日是什么时候？'),
    ('你生日什么时候？', '你生日什么时候？'),
    ('我生日是哪天？', '我生日是哪天？'),
    ('我们第一次见面是哪天？', '我们第一次见面是哪天？'),
    ('咱们什么时候认识的？', '咱们什么时候认识的？'),
    ('你还记得我吗？', '你还记得我吗？'),
    ('我的生日是哪天？', '米拉的生日是哪天？'),
    ('我们的纪念日是哪天？', '我们的纪念日是哪天？'),
    ('咱们的纪念日是哪天？', '咱们的纪念日是哪天？'),
    ('还记得“你的生日是什么时候”这句原话吗？', '还记得“你的生日是什么时候”这句原话吗？'),
    ('请找出`你的生日`这几个字', '请找出`你的生日`这几个字'),
    ('你们的生日', '你们的生日'),
    ('你们和您们什么时候来？', '你们和您们什么时候来？'),
    ('迷你蛋糕和自我介绍', '迷你蛋糕和自我介绍'),
    ('你说“我生日是哪天”是什么意思？', '你说“我生日是哪天”是什么意思？'),
    ('自我的探索和忘我的工作', '自我的探索和忘我的工作'),
    ('迷你的蛋糕', '迷你的蛋糕'),
    ('您的生日是哪天？', '您的生日是哪天？'),
    ('喜欢你做的星星，我想起我们聊过的星云', '喜欢你做的星星，我想起我们聊过的星云'),
])
def test_person_references_keep_quoted_perspectives(query, expected):
    assert resolve_person_references(query, IDENTITY) == expected


def test_unconfigured_names_do_not_invent_a_person():
    assert resolve_person_references('你的生日是哪天', {}) == '你的生日是哪天'
    assert resolve_person_references('你生日是哪天', {}) == '你生日是哪天'


@pytest.mark.parametrize('display_name', [None, '', '   ', '用户'])
def test_user_name_is_used_when_display_name_is_not_configured(display_name):
    from serein.recall.germany.identity import identity_names

    names = identity_names({'identity': {'ai_name': 'Orion', 'user_name': 'Mira',
                                        'user_display_name': display_name}})
    assert resolve_person_references('我的生日是哪天', names) == 'Mira的生日是哪天'
    assert resolve_person_references('你的生日是哪天', names) == 'Orion的生日是哪天'


@pytest.fixture
def birthday_recall(tmp_path, monkeypatch):
    database, index = tmp_path / 'memory.db', tmp_path / 'index.db'
    entities = tmp_path / 'entities.db'
    with sqlite3.connect(entities) as conn:
        conn.execute('CREATE TABLE scope_anchors (entity_text TEXT, entity_key TEXT, arc_key TEXT)')
        conn.execute('CREATE TABLE observed_entities (owner_kind TEXT, owner_id TEXT, entity_key TEXT, '
                     'entity_text TEXT, occurrence_count INTEGER, source_count INTEGER, '
                     'confidence_basis TEXT, scope_eligible INTEGER)')
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'identity': IDENTITY, 'entity_database': str(entities)}), encoding='utf-8')
    profile = dict(model='fixture', provider_host='fixture.invalid', query_instruction='',
                   document_instruction='', max_chars=6000)
    with Store(database) as store:
        store.create('assistant_birthday', 'scene', 'Orion的生日', 'Orion的生日是4月8日。')
        store.create('user_birthday', 'scene', '米拉的生日', '米拉的生日是9月17日。')
    from serein.deployment import save_settings
    save_settings(database, {'identity': {'ai_name': 'Orion', 'user_name': '米拉'}})
    build_index(database, index)
    with sqlite3.connect(index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
        # The wrong person's memory has the higher vector score. Relevance
        # scoring must still receive the intended person's name.
        conn.execute("INSERT INTO vectors VALUES ('assistant_birthday','[0.8,0.6]',2)")
        conn.execute("INSERT INTO vectors VALUES ('user_birthday','[1,0]',2)")
    embedded, ranked = [], []

    class Embedding:
        def __init__(self, *args, **kwargs):
            pass

        def query(self, text, *, client=None):
            embedded.append(text)
            return {'query': text, 'profile': profile, 'embedding': [1, 0]}

    def rank(query, documents):
        ranked.append((query, documents))
        target = 'assistant_birthday' if any(term in query for term in ('Orion的生日', 'Orion生日')) else 'user_birthday'
        return {row['ref']: .9 if row['ref'] == 'scene:' + target else .1 for row in documents}

    monkeypatch.setattr('serein.adapters.embedding.EmbeddingClient', Embedding)
    monkeypatch.setattr('serein.recall.routing.route_query', lambda *args, **kwargs: {
        'route': 'present_chitchat', 'action': 'recall', 'reason': 'uncertain_route',
        'scores': [{'name': 'present_chitchat', 'action': 'skip', 'score': .47, 'threshold': .6}],
    })
    settings = Settings(database, index, embedding={'endpoint': 'unused', 'api_key_env': 'unused'},
                        recall={'routing_file': 'unused', 'germany_policy_file': str(policy)})
    return Recall(settings, reranker=rank), embedded, ranked


@pytest.mark.parametrize('query,target,rank_query', [
    ('你的生日是什么时候？', 'assistant_birthday', 'Orion的生日是什么时候？'),
    ('还记得你的生日是什么时候吗？', 'assistant_birthday', '还记得Orion的生日是什么时候吗？'),
    ('我的生日是哪天？', 'user_birthday', '米拉的生日是哪天？'),
    ('还记得你的生日的原话吗？', 'assistant_birthday', '还记得Orion的生日的原话吗？'),
])
def test_natural_birthday_question_reaches_reranker_without_rewriting_original(birthday_recall, query, target, rank_query):
    engine, embedded, ranked = birthday_recall
    result = engine.run(query, method='semantic', min_cosine=.5, user_utterance=True)
    assert result['query'] == query and embedded == [query]
    assert ranked[0][0] == rank_query
    assert result['admission']['rerank_query'] == rank_query
    assert result['admission']['mode'] == 'direct_evidence_rerank'
    assert result['candidate_retrieval']['candidate_count'] == 2
    assert result['selected_refs'] == ['scene:' + target]
    assert not result['pre_candidate_gate']['applied']
    assert not result['surface_reranker_gate']['applied']


@pytest.mark.parametrize('query', [
    '你生日什么时候？',
    '我生日是哪天？',
    '我们第一次见面是哪天？',
    '喜欢你做的星星，我想起我们聊过的星云',
])
def test_bare_and_shared_references_reach_reranker_unchanged(birthday_recall, query):
    engine, embedded, ranked = birthday_recall

    def rank_original(text, documents):
        ranked.append((text, documents))
        return {row['ref']: .1 for row in documents}

    engine.reranker = rank_original
    result = engine.run(query, method='semantic', min_cosine=.5, user_utterance=True)
    assert result['query'] == query and embedded == [query]
    assert ranked[0][0] == query
    assert result['admission']['rerank_query'] == query
    assert result['candidate_retrieval']['candidate_count'] == 2
    assert not result['pre_candidate_gate']['applied']
    assert not result['surface_reranker_gate']['applied']


def test_personal_question_still_requires_relevant_body_evidence(birthday_recall):
    engine, embedded, ranked = birthday_recall
    engine.reranker = lambda query, rows: {row['ref']: .64 for row in rows}
    result = engine.run('你的生日是什么时候？', method='semantic', min_cosine=.5, user_utterance=True)
    assert result['selected_refs'] == []
    assert all(row['reason'] == 'reranker_below_direct_threshold' for row in result['candidates'])


def test_uncertain_affection_is_judged_by_body_relevance(birthday_recall):
    engine, embedded, ranked = birthday_recall

    def unrelated(query, documents):
        ranked.append((query, documents))
        return {row['ref']: .1 for row in documents}

    engine.reranker = unrelated
    result = engine.run('你的笑容真好看', method='semantic', min_cosine=.5, user_utterance=True)
    assert ranked and not result['pre_candidate_gate']['applied']
    assert result['status'] == 'no_match' and result['cards'] == []
    assert all(row['reason'] == 'reranker_below_direct_threshold' for row in result['candidates'])


def test_tool_authored_queries_do_not_assume_the_user_is_the_speaker(birthday_recall):
    engine, embedded, ranked = birthday_recall
    query = '还记得你的生日是什么时候吗？'
    result = engine.run(query, method='semantic', min_cosine=.5)
    assert ranked[0][0] == query
    assert result['admission']['rerank_query'] == query


def test_hook_marks_its_input_as_the_users_original_utterance():
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from serein.api.gateway import routes

    captured = {}

    def recall(query, **options):
        captured.update(query=query, **options)
        return {'cards': [], 'context': '', 'selected_refs': []}

    app = FastAPI()
    app.include_router(routes(SimpleNamespace(recall=recall), []))
    with TestClient(app) as client:
        response = client.post('/api/hook/recall', json={'query': '你的生日是什么时候？', 'simulation': True})
    assert response.status_code == 200
    assert captured['query'] == '你的生日是什么时候？'
    assert captured['user_utterance'] is True


@pytest.mark.parametrize('query,target,rank_query,fact', [
    ('我的生日是哪天？', 'user_birthday', '米拉的生日是哪天？', '9月17日'),
    ('你的生日是什么时候？', 'assistant_birthday', 'Orion的生日是什么时候？', '4月8日'),
])
def test_gateway_and_hook_resolve_the_same_speaker_and_deliver_body(
        birthday_recall, monkeypatch, query, target, rank_query, fact):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.bootstrap import initialize
    from dataclasses import replace
    from examples.hook_host import SereinHook
    from test_public_settings import configure

    engine, embedded, ranked = birthday_recall
    initialize(engine.settings)
    client = TestClient(create_app(replace(engine.settings, writable=True), token='synthetic', live=True),
                        headers={'Authorization': 'Bearer synthetic'})
    configure(client).raise_for_status()
    monkeypatch.setattr('serein.configured_models.memory_ready', lambda _: True)
    monkeypatch.setattr('serein.application.Services.recall', lambda self, text, **options: engine.run(text, **options))
    model_inputs = []

    def host_model(messages):
        assert fact in json.dumps(messages, ensure_ascii=False)
        assert query in messages[-1]['content']
        model_inputs.append(messages)
        return {'choices': [{'message': {'role': 'assistant', 'content': fact}}]}

    async def complete(model, body, **options):
        return host_model(body['messages'])

    monkeypatch.setattr('serein.api.chat.complete', complete)
    messages = [{'role': 'user', 'content': query}]
    response = client.post('/v1/chat/completions', json={
        'messages': messages, 'serein': {'window_id': 'gateway-window', 'memory': True}})
    response.raise_for_status()
    assert response.json()['choices'][0]['message']['content'] == fact

    hook = SereinHook('https://serein.example', 'synthetic')

    def request(method, path, body=None):
        result = client.request(method, path, json=body)
        result.raise_for_status()
        return result.json()

    monkeypatch.setattr(hook, '_json', request)
    turn = hook.prepare('hook-window', messages)
    assert host_model(turn.messages)['choices'][0]['message']['content'] == fact
    hook.record_success(turn)
    assert len(model_inputs) == 2 and embedded == [query, query]
    assert [row[0] for row in ranked] == [rank_query, rank_query]
    receipts = client.get('/v1/host/deliveries').json()['items']
    assert {(row['window_id'], row['reported_by']) for row in receipts} == {
        ('gateway-window', 'serein_chat_proxy'), ('hook-window', 'host')}
    assert all(row['delivered_ids'] == ['scene:' + target] for row in receipts)


def test_instance_rename_changes_next_rerank_query_without_rewriting_memories(birthday_recall):
    from serein.deployment import save_settings

    engine, embedded, ranked = birthday_recall
    save_settings(engine.settings.database, {'identity': {'ai_name': 'Lyra', 'user_name': 'Nori'}})
    for query, expected in [('你的生日什么时候？', 'Lyra的生日什么时候？'), ('我的生日哪天？', 'Nori的生日哪天？'),
                            ('你生日什么时候？', '你生日什么时候？'), ('我们的纪念日呢？', '我们的纪念日呢？')]:
        result = engine.run(query, method='semantic', min_cosine=.5, user_utterance=True)
        assert embedded[-1] == query and ranked[-1][0] == expected
        assert result['query'] == query and result['admission']['rerank_query'] == expected
    with Store(engine.settings.database, read_only=True) as store:
        assert store.read('assistant_birthday')['body_md'] == 'Orion的生日是4月8日。'
