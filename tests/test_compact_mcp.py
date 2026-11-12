"""Compact public tool catalog with real local source/lifecycle behavior."""
import asyncio
from dataclasses import replace
import re

import pytest

from test_ob_types import runtime, refs
from test_authored_mcp_parity import call
from serein.api.mcp import create_server
from serein.application import Application
from serein.core.feelings import Feelings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions.pipeline_agent import PENDING_TOOLS
from serein.api.typed_authored import INTERNAL_TYPED_TOOLS


@pytest.mark.parametrize('private', [False, True])
def test_feeling_uses_scene_create_edit_status_read_and_lookup(runtime, private):
    server = create_server(Application(runtime), private=private)
    text = call(server, 'write_scene', content='I felt calm.', cues=[], feel_sources=refs())
    key = re.search(r'\[scene_id:([^\]]+)\]', text)[1]
    before = Feelings(runtime.database).read(key)
    assert before['document']['kind'] == 'scene'
    assert not before['document']['manual_surface']
    assert 'type: feel' in call(server, 'read_memory', identifier=key)
    assert key in call(server, 'recall_memory', query='calm', kind='feel', source_id='event_rain')
    assert key not in call(server, 'recall_memory', query='no match', kind='feel')
    result = call(server, 'edit_scene', scene_id=key,
                  expected_updated_at=before['document']['updated_at'], title='Rain', content='Still calm.')
    assert result['status'] == 'updated'
    after = Feelings(runtime.database).read(key)
    assert after['document']['body_md'] == 'Still calm.'
    assert after['document']['metadata']['feel_sources'] == before['document']['metadata']['feel_sources']
    assert after['document']['metadata']['memory_role'] == 'feel'
    assert after['evidence_scope'] == 'subjective_feeling_not_evidence'
    assert call(server, 'edit_scene', scene_id=key, expected_updated_at='stale', content='Lost')['status'] == 'conflict'
    with pytest.raises(Exception):
        call(server, 'edit_scene', scene_id=key, expected_updated_at=after['document']['updated_at'], cues=['fake evidence'])
    call(server, 'set_scene_status', scene_id=key, expected_updated_at=after['document']['updated_at'], status='archived')
    assert Feelings(runtime.database).read(key)['document']['lifecycle'] == 'archived'
    with Store(runtime.database, read_only=True) as store:
        assert store.read('event_rain')['body_md'] == 'We watched the rain together.'


def test_retired_allowlist_does_not_gain_combined_write_permissions(runtime):
    save_settings(runtime.database, {'features': {'pipeline_agent': True}})
    for retired in PENDING_TOOLS | INTERNAL_TYPED_TOOLS:
        server = create_server(Application(replace(runtime, mcp_tools=[retired, 'read_memory'])))
        for _ in range(2):
            assert {tool.name for tool in asyncio.run(server.list_tools())} == {'read_memory'}
        with pytest.raises(Exception, match='Unknown tool'):
            call(server, retired)


def test_compact_catalog_and_invalid_actions_do_not_write(runtime):
    save_settings(runtime.database, {'features': {'pipeline_agent': True}})
    server = create_server(Application(runtime))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert not (PENDING_TOOLS | INTERNAL_TYPED_TOOLS) & names
    assert {'pipeline_pending', 'ob_archive'} <= names
    for args in ({'action': 'unknown'}, {'action': 'finish'}, {'action': 'list', 'pending_id': 1},
                 {'action': 'read', 'pending_id': 1, 'replan': True}, {'unexpected': True}):
        with pytest.raises(Exception):
            call(server, 'pipeline_pending', **args)
    assert call(server, 'ob_archive', namespace='empty')['items'] == []
    with pytest.raises(Exception):
        call(server, 'ob_archive', namespace='empty', record_id='x', genre='feel')
    readonly = create_server(Application(replace(runtime, writable=False)))
    readonly_names = {tool.name for tool in asyncio.run(readonly.list_tools())}
    assert not {'pipeline_pending', 'write_scene', 'edit_scene'} & readonly_names
    assert 'ob_archive' in readonly_names
