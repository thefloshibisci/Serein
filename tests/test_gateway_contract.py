import json

import pytest

from serein.api.surface import SELF_USE_TOOLS, self_use_catalog
from serein.core import Store
from serein.core.reader import Reader
from serein.recall.rendering import render, read_arc_picks
from serein.recall.surface_gate import SurfaceGate


@pytest.mark.parametrize('route', ['present_chitchat', 'present_reality', '技术闲聊', 'custom_skip'])
def test_final_skip_cannot_be_overridden_by_route_category_or_memory_words(route):
    gate = SurfaceGate()
    skip = {'route': route, 'action': 'skip', 'reason': 'published_skip_route'}
    for query in ('你好', '还记得那次吗', '请读《雨天相逢》', '为什么叫Astra'):
        assert gate.before_candidates(query, skip)['applied']
    assert gate.engine._typed_surface_reranker_gate('这是什么意思',
        {'scope_anchor': {'arc_key': 'fixture'}}, gate.debug(skip),
        candidates=[{'candidate_sources': ['scene_cue_candidate']}],
        owner_entity_matches=[{'owner_id': 'event_a'}])['applied']


def test_uncertain_route_is_not_overridden_by_the_winning_skip_template():
    gate = SurfaceGate()
    decision = {'route':'present_chitchat', 'action':'recall', 'reason':'uncertain_route',
                'scores':[{'name':'present_chitchat','action':'skip','score':.43,'threshold':.6}]}
    debug = gate.debug(decision)
    assert debug['route_action'] == debug['applied_action'] == 'recall'
    assert debug['template_action'] == 'skip'
    before = gate.before_candidates('那个安排还算数吧', decision)
    assert not before['applied'] and before['route_action'] == 'recall'
    assert before['template_action'] == 'skip'
    assert not gate.before_candidates('还记得那次吗', decision)['applied']
    assert not gate.after_candidates('这是什么意思', decision, [])['applied']
    technical = {**decision, 'route':'技术闲聊',
                 'scores':[{'name':'技术闲聊','action':'skip','score':.43,'threshold':.6}]}
    assert not gate.before_candidates('正在修自动切分的bug', technical)['applied']


def test_cards_and_arc_menu_are_readable_without_implicitly_reading_story(tmp_path):
    db = tmp_path / 'cards.db'
    with Store(db) as store:
        store.create('scene_a', 'scene', '雨天', '## Scene\n窗边的雨\n第二行', metadata={'date': '2026-09-07'})
        store.create('narrative_a', 'narrative', '雨的故事', '不该自动进入窗口的整卷', metadata={'arc_key': 'rain'})
        store.conn.execute("INSERT INTO narrative_materials VALUES ('narrative_a',1,'test','scene','scene_a','linked','{}')")
        source = store.add_source('original', '不该自动进入卡片的原文')
        store.bind('scene_a', source)
    with Reader(db) as reader:
        hit = {'id': 'scene_a', 'kind': 'scene', 'score': .527891, 'object': reader.read('scene_a')}
        result = render([hit], reader=reader)
        card = result['cards'][0]
        assert set(card) == {'id', 'source', 'source_kind', 'title', 'text', 'score', 'render_shape', 'date', 'date_basis'}
        assert card['date'] == '2026-09-07'
        assert 'date: 2026-09-07' in result['additional_context']
        assert card['score'] == .5279 and card['text'] == '窗边的雨 第二行'
        assert 'Arc: 雨的故事 (key=rain)' in result['context']
        assert '[0] narrative: 雨的故事' in result['context']
        assert 'read_arc_materials(arc_key="rain", picks=[编号])' in result['context']
        assert '不该自动进入' not in json.dumps(result, ensure_ascii=False)
        assert not result['injected']
        assert read_arc_picks(reader, 'rain', [1])['items'][0]['object']['id'] == 'scene_a'
        assert read_arc_picks(reader, 'rain', [0])['items'][0]['object']['document']['body_md'] == '不该自动进入窗口的整卷'
        assert render([hit], reader=reader, delivered_menu_keys=['rain'])['menus_suppressed'] == ['rain']
        with pytest.raises(ValueError):
            read_arc_picks(reader, 'rain', [99])


def test_self_use_catalog_excludes_evidence_and_window_narrative_authoring():
    excluded = ('bind_scene_evidence', 'unbind_scene_evidence', 'read_scene_evidence',
                'narrative_revision_inbox', 'review_narrative_revision', 'publish_narrative',
                'close_window', 'revise_window_shadow')
    tools = [{'name': name} for name in (*SELF_USE_TOOLS, *excluded)]
    assert len(self_use_catalog(tools)) == 19
    assert not set(excluded) & {row['name'] for row in self_use_catalog(tools)}

@pytest.fixture
def arc_upload_database(tmp_path):
    db = tmp_path / 'arc-uploads.db'
    with Store(db) as store:
        store.create('narrative_uploads', 'narrative', '合成测试卷', '仅显式读取的卷正文',
                     metadata={'arc_key': 'work:synthetic'})
        for number in range(8):
            key = f'scene_{number}'
            store.create(key, 'scene', f'场景{number}', f'场景正文{number}', metadata={'date': '2026-01-01'})
            store.conn.execute("INSERT INTO narrative_materials VALUES ('narrative_uploads',1,'test','scene',?,'linked','{}')", (key,))
        for key, disposition in [('upload_selected', 'linked'), ('upload_excluded', 'linked'),
                                  ('upload_excluded', 'excluded'), ('upload_mentioned', 'mentioned'),
                                  ('upload_missing', 'linked')]:
            store.conn.execute("INSERT INTO narrative_materials VALUES ('narrative_uploads',1,?,'upload',?,?,'{}')",
                               (disposition, key, disposition))
        for key in ('upload_selected', 'upload_excluded', 'upload_mentioned'):
            body = '长文原文😀\n"引用"\\' * 900
            store.save_import_record('synthetic', key + '.md', body.encode())
            store.conn.execute('INSERT INTO narrative_uploads VALUES (?,?,?,?)',
                (key, 'synthetic', key + '.md', json.dumps({'filename': key + '.md', 'extracted_text': body})))
    return db


def test_linked_upload_is_numbered_and_explicitly_readable_without_body_injection(arc_upload_database):
    with Reader(arc_upload_database) as reader:
        hit = {'id': 'scene_0', 'kind': 'scene', 'score': .9, 'object': reader.read('scene_0')}
        rendered = render([hit], reader=reader)
        assert '[9] upload: upload_selected.md' in rendered['context']
        assert 'upload_excluded' not in rendered['context']
        assert 'upload_mentioned' not in rendered['context']
        assert 'upload_missing' not in rendered['context']
        assert '长文原文' not in json.dumps(rendered, ensure_ascii=False)
        picked = read_arc_picks(reader, 'work:synthetic', [9])['items'][0]
        assert picked['id'] == 'upload_selected' and picked['kind'] == 'upload'
        assert picked['object'] == reader.read('upload_selected', kind='upload', with_evidence=False)
        assert reader.store.conn.total_changes == 0
