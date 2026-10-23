import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serein.api.gateway import routes
from serein.core import Store
from serein.core.reader import Reader
from serein.recall.rendering import render


@pytest.mark.parametrize('kind', ['scene', 'event'])
def test_hook_keeps_memory_id_and_reads_all_evidence_on_demand(tmp_path, kind):
    database = tmp_path / 'memory.db'
    with Store(database) as store:
        store.create('memory', kind, '那段记忆', '记忆正文。')
        for message, session in [('7', '24'), ('8', '24'), ('7', '25')]:
            source = store.add_source(json.dumps(['assistant_bridge', session, message]),
                '不应注入的绑定原文', metadata={'source_system': 'assistant_bridge', 'session_id': session, 'message_id': message})
            store.bind('memory', source)
        no_id = store.add_source('legacy-opaque-key', '也不应注入的匿名原文')
        store.bind('memory', no_id)
        # A numeric ID can coincidentally appear in a rendered timestamp.
        removed_message_id = 'removed-message-id-must-not-be-injected'
        removed = store.add_source('removed', '已经解绑的原文', metadata={'message_id': removed_message_id})
        store.bind('memory', removed)
        store.conn.execute('UPDATE evidence_bindings SET active=0 WHERE source_id=?', (removed,))
    with Reader(database) as reader:
        obj = reader.read('memory', with_evidence=False)
        assert obj['evidence'] == []
        result = render([{'kind': kind, 'id': 'memory', 'object': obj}], reader=reader)
        proof = reader.read(f'{kind}:memory', with_evidence=True)
        assert len(proof['evidence']) == 4
        assert {e['content'] for e in proof['evidence']} == {'不应注入的绑定原文', '也不应注入的匿名原文'}
        assert proof['document']['body_md'] == '记忆正文。'
        assert reader.store.conn.total_changes == 0
    card = result['cards'][0]
    assert card['id'] == f'{kind}:memory'
    assert 'source_refs' not in card
    app = FastAPI()
    app.include_router(routes(SimpleNamespace(recall=lambda *a, **kw: {**result, 'selected_refs': [f'{kind}:memory']}), []))
    with TestClient(app) as client:
        data = client.post('/api/hook/recall', json={'query': '那段记忆'}).json()
    for context in (result['context'], result['additional_context'], result['full_additional_context'], data['additional_context']):
        assert f'{kind}:memory' in context
        assert 'read_memory(' in context and 'with_evidence=True' in context
        assert 'source_refs' not in context
        assert '不应注入的绑定原文' not in context and removed_message_id not in context and 'legacy-opaque-key' not in context
