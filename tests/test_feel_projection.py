"""Synthetic projection checks: no backlink scan and no frozen-source leakage."""
from test_live_clients import live
from serein.core.store import Store
from serein.core.feelings import Feelings
from serein.core.personal import Personal


def test_projection_preserves_feel_semantics_and_frozen_sources_without_backlinks(live, monkeypatch):
    settings, client = live
    with Store(settings.database) as store:
        store.create('event_trigger', 'event', 'Original source', 'The original event body.')
    feel = Feelings(settings.database).create('A subjective feeling.', [{'kind': 'event', 'id': 'event_trigger'}], author='Fixture')
    key = feel['id']
    Personal(settings.database).save('annotation', 'note_feel', {'content': 'Still a note', 'author': 'User', 'role': 'user'}, document_id=key)
    def forbidden(*args, **kwargs):
        raise AssertionError('Projection must not call backlinks')
    monkeypatch.setattr('serein.core.feelings.backlinks', forbidden)
    monkeypatch.setattr('serein.core.plans.backlinks', forbidden)
    with Store(settings.database) as store:
        store.revise('event_trigger', expected_revision=1, title='Changed source', body_md='The changed event body.')
    def projected():
        response = client.post('/api/serein/memory-projection', json={})
        assert response.status_code == 200
        return next(row for row in response.json()['scenes'] if row['source_id'] == key)
    row = projected()
    assert row['memory_role'] == row['semantic_kind'] == 'feel'
    assert row['revision'] == 1 and row['evidence_count'] == 0
    assert row['feel_sources'][0]['summary'] == 'The original event body.'
    assert row['feel_sources'][0]['revision'] == 1
    assert row['annotations'][0]['content'] == 'Still a note'
    with Store(settings.database) as store:
        source = store.read('event_trigger')
        store.revise('event_trigger', expected_revision=source['revision'], title=source['title'],
                     body_md=source['body_md'], metadata={'locked': True})
    source = projected()['feel_sources'][0]
    assert source['status'] == 'restricted'
    assert 'summary' not in source and 'title' not in source
