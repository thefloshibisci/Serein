"""MCP/HTTP can settle read pending material without deleting its originals."""
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from test_public_features import settings, ingest, synthetic_runner
from test_pipeline_pending import retained, parked, gated
from test_pipeline_pending_archive import snapshot
from serein.application import Application
from serein.api.http import create_app
from serein.api.mcp import create_server
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p, pipeline_pending as pending
from serein.extensions.pipeline_agent import PENDING_TOOLS, active_materials, tools_for


def result_value(result):
    return result[1] if isinstance(result, tuple) else json.loads(result[0].text)


def test_mcp_lists_reads_finishes_with_atomic_receipt_and_originals_intact(settings):
    item = retained(settings)
    before = snapshot(settings)
    save_settings(settings.database, {'features': {'pipeline_agent': True}})
    server = create_server(Application(settings))

    async def exercise():
        catalog = {tool.name: tool for tool in await server.list_tools()}
        assert 'pipeline_pending' in catalog and not PENDING_TOOLS & catalog.keys()
        assert not catalog['pipeline_pending'].annotations.readOnlyHint
        assert not catalog['pipeline_pending'].annotations.idempotentHint
        page = result_value(await server.call_tool('pipeline_pending', {'action': 'list', }))
        assert page['total'] == 1 and [row['id'] for row in page['items']] == [item['id']]
        first = result_value(await server.call_tool('pipeline_pending', {'action': 'read', 'pending_id': item['id'], 'limit': 1}))
        second = result_value(await server.call_tool('pipeline_pending', {'action': 'read', 'pending_id': item['id'], 'offset': first['next_offset']}))
        assert first['messages'] + second['messages'] == pending.detail(settings.database, item['id'])['messages']
        assert second['next_offset'] is None
        with pytest.raises(Exception):
            await server.call_tool('pipeline_pending', {'action': 'finish', 'pending_id': item['id'], 'confirm': 'wrong'})
        receipt = result_value(await server.call_tool('pipeline_pending',
            {'action': 'finish', 'pending_id': item['id'], 'confirm': pending.ARCHIVE_CONFIRM}))
        assert receipt['status'] == 'archived_only'
        assert receipt['closure_receipt']['originals_preserved']
        assert receipt == result_value(await server.call_tool('pipeline_pending',
            {'action': 'finish', 'pending_id': item['id'], 'confirm': pending.ARCHIVE_CONFIRM}))
        assert result_value(await server.call_tool('pipeline_pending', {'action': 'list', })) == {'items': [], 'total': 0, 'next_before_id': None}
        assert result_value(await server.call_tool('pipeline_pending', {'action': 'read', 'pending_id': item['id']}))['messages'] == pending.detail(settings.database, item['id'])['messages']
    asyncio.run(exercise())
    assert snapshot(settings) == before


def test_http_mcp_protocol_exposes_and_finishes_the_same_pending_item(settings, monkeypatch):
    item = retained(settings)
    before = snapshot(settings)
    save_settings(settings.database, {'features': {'pipeline_agent': True}})
    @asynccontextmanager
    async def lifecycle(app):
        yield
    monkeypatch.setattr('serein.lifecycle.lifespan', lifecycle)
    headers = {'Authorization': 'Bearer test', 'Accept': 'application/json, text/event-stream'}
    with TestClient(create_app(settings, token='test', live=True), headers=headers) as client:
        def rpc(method, params=None):
            response = client.post('/serein/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
            assert response.status_code == 200, response.text
            return response.json()['result']
        def call(name, arguments):
            result = rpc('tools/call', {'name': name, 'arguments': arguments})
            assert not result['isError'], result
            return result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert 'pipeline_pending' in {tool['name'] for tool in rpc('tools/list')['tools']}
        assert not PENDING_TOOLS & {tool['name'] for tool in rpc('tools/list')['tools']}
        assert call('pipeline_pending', {'action': 'list', })['total'] == 1
        originals = call('pipeline_pending', {'action': 'read', 'pending_id': item['id']})['messages']
        assert [row['id'] for row in originals] == item['source_ids']
        args = {'pending_id': item['id'], 'confirm': pending.ARCHIVE_CONFIRM}
        result = call('pipeline_pending', {'action': 'finish', **args})
        assert call('pipeline_pending', {'action': 'finish', **args}) == result
        assert call('pipeline_pending', {'action': 'list', })['total'] == 0
        assert call('pipeline_pending', {'action': 'read', 'pending_id': item['id']})['messages'] == originals
        restored = call('pipeline_pending', {'action': 'reopen', 'pending_id': item['id'], 'confirm': pending.REOPEN_CONFIRM})
        assert restored['pending_id'] != item['id']
        assert call('pipeline_pending', {'action': 'reopen', 'pending_id': item['id'], 'confirm': pending.REOPEN_CONFIRM}) == restored
        assert [row['id'] for row in call('pipeline_pending', {'action': 'list', })['items']] == [restored['pending_id']]
    assert snapshot(settings) == before


def test_mcp_resume_returns_real_frozen_writer_and_settles_selected_item(settings):
    item = retained(settings)
    save_settings(settings.database, {'features': {'pipeline_agent': True}, 'pipeline': {'execution_mode': 'agent'}})
    server = create_server(Application(settings))
    async def exercise():
        result = result_value(await server.call_tool('pipeline_pending', {'action': 'resume', 'pending_id': item['id']}))
        assert result['status'] == 'resumed'
        task = result_value(await server.call_tool('pipeline_next', {'include_recent': True}))
        assert task['status'] == 'awaiting_agent' and task['role'] == 'event_writer'
        with Store(settings.database, read_only=True) as store:
            request = json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE id=?', (task['job_id'],)).fetchone()[0])
        output = await synthetic_runner('event_writer', request)
        await server.call_tool('pipeline_submit', {'job_id': task['job_id'], 'output': output})
        result = result_value(await server.call_tool('pipeline_next', {'include_recent': True}))
        assert result['status'] == 'processed' and result['events'] == 1
        assert result['pending_count'] == 0
        assert result_value(await server.call_tool('pipeline_pending', {'action': 'list', }))['total'] == 0
    asyncio.run(exercise())


def test_next_points_at_pending_tools_instead_of_an_unactionable_wait(settings, monkeypatch):
    item = retained(settings)
    monkeypatch.setattr(p, 'advance', AsyncMock(return_value={'status': 'current'}))
    result = asyncio.run(tools_for(settings)['pipeline_next']())
    assert result['pending_count'] == 1 and result['pending_material_ids'] == [item['id']]
    assert 'pipeline_pending(action=list/read)' in result['pending_instructions']
    assert not any(name in result['pending_instructions'] for name in PENDING_TOOLS)


def test_explicit_agent_resume_reviews_old_round_wait_now(settings):
    item = parked(settings, chars=400)
    result = asyncio.run(tools_for(settings)['pipeline_pending_resume'](item['id']))
    assert result['status'] == 'resumed'
    with Store(settings.database, read_only=True) as store:
        data = json.loads(store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (result['batch_id'],)).fetchone()[0])
    assert data['round_gate_expired'] is True and data['input_policy']['round_gate_enabled'] is False
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=gated))
    assert result['events'] == 1 and active_materials(settings.database)['total'] == 0
    assert p.read_settings(settings.database)['pipeline']['round_gate_enabled'] is True


def test_pending_tools_share_feature_gate_allowlist_and_http(settings):
    item = retained(settings)
    server = create_server(Application(settings))
    async def exercise():
        assert not (PENDING_TOOLS | {'pipeline_pending'}) & {tool.name for tool in await server.list_tools()}
        save_settings(settings.database, {'features': {'pipeline_agent': True}})
        assert 'pipeline_pending' in {tool.name for tool in await server.list_tools()}
        assert not PENDING_TOOLS & {tool.name for tool in await server.list_tools()}
        save_settings(settings.database, {'features': {'pipeline_agent': False}})
        for name in PENDING_TOOLS | {'pipeline_pending'}:
            with pytest.raises(Exception, match='Unknown tool'):
                await server.call_tool(name, {})
    asyncio.run(exercise())
    save_settings(settings.database, {'features': {'pipeline_agent': True}})
    restricted = create_server(Application(replace(settings, mcp_tools=['pipeline_pending'])))
    assert {tool.name for tool in asyncio.run(restricted.list_tools())} == {'pipeline_pending'}
    readonly = create_server(Application(replace(settings, writable=False)))
    assert not (PENDING_TOOLS | {'pipeline_pending'}) & {tool.name for tool in asyncio.run(readonly.list_tools())}
    client = TestClient(create_app(settings, token='test', live=True), headers={'Authorization': 'Bearer test'})
    assert client.post('/v1/extensions/pipeline_pending_list', json={}).json()['total'] == 1
    result = client.post('/v1/extensions/pipeline_pending_finish', json={'pending_id': item['id'], 'confirm': pending.ARCHIVE_CONFIRM})
    assert result.status_code == 200 and result.json()['status'] == 'archived_only'
    assert client.post('/v1/extensions/pipeline_pending_list', json={}).json()['items'] == []
    save_settings(settings.database, {'features': {'pipeline_agent': False}})
    for name in PENDING_TOOLS:
        assert client.post('/v1/extensions/' + name, json={}).status_code == 404


def test_pending_pagination_never_counts_finished_rows_and_empty_db_is_read_only(settings):
    assert active_materials(settings.database) == {'items': [], 'total': 0, 'next_before_id': None}
    item = retained(settings)
    with Store(settings.database) as store:
        for index in range(2, 6):
            store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json,status,result_json) VALUES (?,?,?, ?,?)',
                               ('synthetic-' + str(index), 'synthetic', '{}', 'done', '{}'))
            store.conn.execute('INSERT INTO pipeline_pending_materials(batch_id,scope,status,kind,source_ids_json,reason,updated_at) VALUES (?,?,?,?,?,?,?)',
                               ('synthetic-' + str(index), 'synthetic', 'archived_only' if index == 5 else 'open', 'writing', '[]', 'synthetic', '2030-01-01'))
    first = active_materials(settings.database, limit=2)
    second = active_materials(settings.database, limit=2, before_id=first['next_before_id'])
    assert first['total'] == second['total'] == 4
    assert len(first['items'] + second['items']) == 4 and second['next_before_id'] is None
    assert second['items'][-1]['id'] == item['id']


def test_finish_obeys_pipeline_lease_and_shared_source_safety(settings):
    from serein.work_tasks import execute
    item = retained(settings)
    agent = tools_for(settings)
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def held():
            entered.set()
            await release.wait()
            return {'status': 'current'}
        task = asyncio.create_task(execute(settings.database, 'pipeline', held))
        await entered.wait()
        try:
            assert (await agent['pipeline_pending_finish'](item['id'], pending.ARCHIVE_CONFIRM))['status'] == 'busy'
        finally:
            release.set()
            await task
    asyncio.run(exercise())
    assert active_materials(settings.database)['total'] == 1
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET text='Changed original' WHERE id=?", (item['source_ids'][0],))
    before = snapshot(settings)
    with pytest.raises(Exception, match='原话已变化'):
        asyncio.run(agent['pipeline_pending_finish'](item['id'], pending.ARCHIVE_CONFIRM))
    assert snapshot(settings) == before and active_materials(settings.database)['total'] == 1


@pytest.mark.parametrize('round_gate', [False, True])
def test_reopen_preserves_old_receipt_and_originals_then_saves_one_event(settings, round_gate):
    item = parked(settings, chars=400) if round_gate else retained(settings)
    finished = pending.archive(settings.database, item['id'], pending.ARCHIVE_CONFIRM)
    before = snapshot(settings)
    agent = tools_for(settings)
    result = asyncio.run(agent['pipeline_pending_reopen'](item['id'], pending.REOPEN_CONFIRM))
    assert result['status'] == 'reopened' and result['pending_id'] != item['id']
    assert result == asyncio.run(agent['pipeline_pending_reopen'](item['id'], pending.REOPEN_CONFIRM))
    assert snapshot(settings) == before
    assert pending.detail(settings.database, item['id'])['closure_receipt'] == finished['closure_receipt']
    assert [row['id'] for row in active_materials(settings.database)['items']] == [result['pending_id']]
    asyncio.run(agent['pipeline_pending_resume'](result['pending_id'], replan=True))
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=gated if round_gate else synthetic_runner))['events'] == 1
    assert active_materials(settings.database)['total'] == 0
    assert pending.detail(settings.database, item['id'])['closure_receipt'] == finished['closure_receipt']
    # A retried old undo cannot reopen the newly completed Event.
    assert asyncio.run(agent['pipeline_pending_reopen'](item['id'], pending.REOPEN_CONFIRM)) == result
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 1
        assert [row[0] for row in store.conn.execute('SELECT outcome FROM raw_processing')] == ['settled', 'settled']


def test_reopen_keeps_saved_bridge_event_and_bindings_while_restoring_unsettled_ownership(settings):
    from test_track_continuation_parity import _two_track_bridge_batch
    from serein.extensions import pipeline_latest as latest
    batch, data, first, _, first_component, second_component = _two_track_bridge_batch(settings)
    first_plan = latest.normalize_event_curator_output({
        'events': [{'action': 'create', 'primary_track_id': first,
                    'base_event_ids': [], 'owned_unit_roots': [1, 3]}],
        'skip_unit_roots': [], 'defer_unit_roots': [],
        'decision_review': {'events': [{'event_index': 0, 'reason': 'Synthetic owned activity'}],
                            'boundaries': [], 'dispositions': []},
    }, first_component)
    # The other corridor has not settled the shared bridge, even though the
    # accepted Event can already cite it. This is settle's existing contract.
    second_plan = {'events': [], 'defer_source_message_ids': [2, 3],
                   'skip_source_message_ids': [4], 'hard_skips': []}
    written = {'title': 'Synthetic', 'event_draft': 'Synthetic Event',
               'recallable': True, 'evidence_sufficient': True}
    result = p.settle(settings.database, batch, data, data['routing_result'], [
        (first_component, first_plan, [(first_plan['events'][0], written)]),
        (second_component, second_plan, []),
    ])
    assert result['events'] == 1 and result['pending'] == 2
    def saved_memory():
        with Store(settings.database, read_only=True) as store:
            return {table: [tuple(row) for row in store.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('raw_events', 'documents', 'fact_events', 'fact_event_sources',
                                  'sources', 'pipeline_event_details', 'pipeline_track_events')}
    with Store(settings.database, read_only=True) as store:
        item = pending.entries(store.conn)[0]
        assert item['source_ids'] == [2, 3]
        assert store.conn.execute("SELECT 1 FROM fact_event_sources WHERE message_id='3'").fetchone()
        assert dict(store.conn.execute('SELECT raw_id,outcome FROM raw_processing')) == {1: 'settled', 4: 'skipped'}
    before = saved_memory()
    receipt = pending.archive(settings.database, item['id'], pending.ARCHIVE_CONFIRM)
    reopened = asyncio.run(tools_for(settings)['pipeline_pending_reopen'](item['id'], pending.REOPEN_CONFIRM))
    assert reopened['source_ids'] == [2, 3] and reopened['pending_id'] != item['id']
    assert saved_memory() == before
    assert pending.detail(settings.database, item['id'])['closure_receipt'] == receipt['closure_receipt']
    with Store(settings.database, read_only=True) as store:
        assert dict(store.conn.execute('SELECT raw_id,outcome FROM raw_processing')) == {1: 'settled', 4: 'skipped'}
    assert active_materials(settings.database)['total'] == 1


@pytest.mark.parametrize('drift', ['raw', 'receipt', 'processed', 'operation', 'shared', 'inflight'])
def test_reopen_refuses_drift_or_sources_owned_elsewhere(settings, drift):
    item = retained(settings)
    pending.archive(settings.database, item['id'], pending.ARCHIVE_CONFIRM)
    with Store(settings.database) as store:
        if drift == 'raw':
            store.conn.execute("UPDATE raw_events SET text='Changed' WHERE id=?", (item['source_ids'][0],))
        elif drift == 'receipt':
            store.conn.execute("UPDATE pipeline_pending_archive_receipts SET receipt_json=json_set(receipt_json,'$.source_sha256','{}') WHERE pending_id=?", (item['id'],))
        elif drift == 'processed':
            store.conn.execute("UPDATE raw_processing SET outcome='settled' WHERE raw_id=?", (item['source_ids'][0],))
        elif drift == 'operation':
            store.conn.execute("UPDATE raw_processing SET operation_id='another-owner' WHERE raw_id=?", (item['source_ids'][0],))
        else:
            store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES (?,?,?,?)',
                ('other', 'other', json.dumps({'messages': [{'id': item['source_ids'][0]}]}) if drift == 'inflight' else '{}', 'waiting_materials'))
            if drift == 'shared':
                store.conn.execute("INSERT INTO pipeline_pending_materials(batch_id,scope,status,kind,source_ids_json,reason,updated_at) VALUES ('other','other','open','writing',?,'shared','2030-01-01')", (json.dumps(item['source_ids']),))
    before = snapshot(settings)
    with pytest.raises(Exception):
        asyncio.run(tools_for(settings)['pipeline_pending_reopen'](item['id'], pending.REOPEN_CONFIRM))
    assert snapshot(settings) == before
    with Store(settings.database, read_only=True) as store:
        assert not store.conn.execute("SELECT 1 FROM background_state WHERE name LIKE 'pending_reopen:%'").fetchone()


def test_reopen_rolls_back_if_its_recovery_receipt_cannot_be_saved(settings):
    item = retained(settings)
    receipt = pending.archive(settings.database, item['id'], pending.ARCHIVE_CONFIRM)
    with Store(settings.database) as store:
        store.conn.execute("CREATE TRIGGER reject_reopen BEFORE INSERT ON background_state "
            "WHEN NEW.name LIKE 'pending_reopen:%' BEGIN SELECT RAISE(ABORT,'synthetic recovery receipt failure'); END")
        before = {table: [tuple(row) for row in store.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                  for table in ('raw_processing', 'pipeline_batches', 'pipeline_pending_materials', 'pipeline_pending_archive_receipts')}
    with pytest.raises(Exception, match='synthetic recovery receipt failure'):
        pending.reopen(settings.database, item['id'], pending.REOPEN_CONFIRM)
    with Store(settings.database, read_only=True) as store:
        after = {table: [tuple(row) for row in store.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                 for table in before}
    assert after == before
    assert pending.detail(settings.database, item['id'])['closure_receipt'] == receipt['closure_receipt']


def test_scheduler_never_automatically_finishes_or_reopens_old_material(settings, monkeypatch):
    from test_route_scope_holds import Clock
    item = parked(settings, chars=400)
    before = snapshot(settings)
    monkeypatch.setattr(p, 'datetime', Clock)
    def forbidden(*args, **kwargs):
        pytest.fail('Retired waiting/expiry machinery ran')
    monkeypatch.setattr(pending, 'settle_stale', forbidden, raising=False)
    monkeypatch.setattr(pending, 'archive_stale', forbidden, raising=False)
    for _ in range(3):
        assert asyncio.run(p.scheduled_advance(settings.database))['status'] in ('current', 'settled_today')
    assert snapshot(settings) == before
    assert active_materials(settings.database)['items'][0]['id'] == item['id']
    with Store(settings.database, read_only=True) as store:
        assert not store.conn.execute('SELECT 1 FROM pipeline_pending_archive_receipts').fetchone()
        assert not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_pending_expiry_reviews'").fetchone()


def test_agent_finish_holds_lease_until_cancelled_disk_work_finishes(settings, monkeypatch):
    import threading
    from serein import work_tasks
    item = retained(settings)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocked_archive(*args):
        started.set()
        try:
            assert release.wait(3)
            return {'status': 'archived_only'}
        finally:
            finished.set()
    monkeypatch.setattr(pending, 'archive', blocked_archive)
    async def scenario():
        task = asyncio.create_task(tools_for(settings)['pipeline_pending_finish'](item['id'], pending.ARCHIVE_CONFIRM))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.01)
            assert started.is_set()
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            async def forbidden():
                pytest.fail('Lease released while disk work was still active')
            assert (await work_tasks.execute(settings.database, 'pipeline', forbidden))['status'] == 'busy'
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
