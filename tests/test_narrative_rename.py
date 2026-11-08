import pytest

from test_live_clients import live
from test_live_narratives import seed
from serein.compat.narratives import narrative_transaction


@pytest.mark.parametrize('collecting', [False, True])
def test_rename_preserves_prose_subtitle_materials_and_theme(live, collecting):
    settings, client = live
    event, scene = seed(client, settings)
    with narrative_transaction(settings.database, write=True) as rolls:
        rolls.publish(narrative_id='narrative_test', expected_revision=1, title='旧书名 / 窗边',
            document=f'# 旧书名 / 窗边\n\n## 第一人称叙事\n\n' + ('' if collecting else '记下雨声。\n\n')
                + f'## 材料\n{event}\n{scene}\n',
            publication_status='collecting' if collecting else 'reviewed',
            current_status_cue='主题：一起听雨', arc_key='arc:rain',
            source_event_ids=[event], source_scene_ids=[scene])
        # Renaming must also work when an old material is no longer readable.
        rolls.store.set_lifecycle(scene, 'archived')
        before = rolls.read('narrative_test')
        source = rolls.store.read(scene)
    request = {'narrative_id': 'narrative_test', 'expected_revision': before['revision'], 'title': '新书名'}
    result = client.post('/api/narrative-rolls/rename', json=request)
    assert result.status_code == 200, result.text
    renamed = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    assert renamed['title'] == '新书名 / 窗边'
    assert renamed['revision'] == before['revision'] + 1
    assert renamed['full_document'].split('\n', 1)[1] == before['full_document'].split('\n', 1)[1]
    for field in ('body', 'publication_status', 'current_status_cue', 'arc_key', 'published_at',
                  'linked_event_ids', 'linked_scene_ids', 'linked_diary_ids'):
        assert renamed[field] == before[field]
    with narrative_transaction(settings.database) as rolls:
        assert rolls.store.read(scene) == source
        assert rolls.read('narrative_test')['history'][-1]['title'] == before['title']
    assert client.post('/api/narrative-rolls/rename', json=request).status_code == 409
    assert client.post('/api/narrative-rolls/rename', json={**request, 'expected_revision': renamed['revision']}).json()['status'] == 'unchanged'


@pytest.mark.parametrize('title', ['', '雨' * 17, '一\n二', '# 雨声'])
def test_invalid_title_does_not_change_the_book(live, title):
    settings, client = live
    seed(client, settings)
    response = client.post('/api/narrative-rolls/rename', json={
        'narrative_id': 'narrative_test', 'expected_revision': 1, 'title': title})
    assert response.status_code == 400
    current = client.get('/api/narrative-rolls?narrative_id=narrative_test').json()
    assert current['title'] == '雨声' and current['revision'] == 1
