import asyncio
import json
from datetime import datetime, timedelta, timezone

from test_public_features import settings, raw_archive, output_for
from serein import work_tasks
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.extensions import pipeline_recovery as recovery
from serein.core.store import encode
import pytest


def gated_output(role, request):
    output = output_for(role, request)
    if role == 'event_curator':
        for row in output['decision_review']['events']:
            row['admission'] = {'closed_by': None}
    return output


def seed(settings, session, day, rounds=1):
    stamp = datetime(2025, 1, day, tzinfo=timezone.utc)
    raw_archive(settings).ingest([
        {'source_event_id': f'{session}-{day}-{n}-{role}', 'session_id': session,
         'role': role, 'text': f'Book club plan {session} {day}',
         'created_at': (stamp + timedelta(minutes=n)).isoformat()}
        for n, role in enumerate(('user', 'assistant') * rounds)
    ], source='test')


def test_worker_drains_past_deferred_chunk_and_independent_scope(settings, monkeypatch):
    seed(settings, 'first', 1)  # Deferred, but must not block the next day in this scope.
    seed(settings, 'first', 2, rounds=2)
    seed(settings, 'independent', 3, rounds=2)
    save_settings(settings.database, {'pipeline': {'round_gate_enabled': True}})
    calls = []
    native = p._advance

    async def runner(role, request):
        if role == 'event_curator':
            ids = [m['id'] for m in request['component']['messages']]
            calls.append(ids)
        return gated_output(role, request)

    async def advance(database, **kwargs):
        return await native(database, runner=runner, **kwargs)

    monkeypatch.setattr(p, '_advance', advance)
    result = asyncio.run(work_tasks.work(settings, 'pipeline', {'include_recent': True}))
    assert result['status'] == 'current' and result['events'] == 2
    assert calls == [[1, 2], [3, 4, 5, 6], [7, 8, 9, 10]]
    with Store(settings.database, read_only=True) as store:
        assert [r[0] for r in store.conn.execute('SELECT raw_id FROM raw_processing ORDER BY raw_id')] == [3, 4, 5, 6, 7, 8, 9, 10]
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0] == 2
        receipt = json.loads(store.conn.execute("SELECT result_json FROM pipeline_batches WHERE status='done' ORDER BY rowid LIMIT 1").fetchone()[0])
        assert receipt['attempted_source_message_ids'] == [1, 2]
    # A new Continue with unchanged material makes no new paid requests.
    assert asyncio.run(work_tasks.work(settings, 'pipeline', {}))['status'] == 'current'
    assert len(calls) == 3


def test_all_deferred_is_bounded_and_new_material_remains_eligible(settings, monkeypatch):
    seed(settings, 'a', 1)
    seed(settings, 'b', 2)
    save_settings(settings.database, {'pipeline': {'round_gate_enabled': True}})
    calls = []
    native = p._advance

    async def runner(role, request):
        if role == 'event_curator':
            ids = [m['id'] for m in request['component']['messages']]
            calls.append(ids)
            assert len(calls) <= 3
        return gated_output(role, request)

    async def advance(database, **kwargs):
        return await native(database, runner=runner, **kwargs)

    monkeypatch.setattr(p, '_advance', advance)
    result = asyncio.run(work_tasks.work(settings, 'pipeline', {}))
    assert result['status'] == 'current' and result['events'] == 0, result
    assert calls == [[1, 2], [3, 4]]
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
    assert p.new_batch(settings.database, True) is None
    seed(settings, 'a', 3)
    next_batch = p.new_batch(settings.database, True)
    assert [m['id'] for m in json.loads(next_batch['input_json'])['messages']] == [5, 6]


def test_retained_material_requires_explicit_resume_after_day_or_policy_change(settings):
    from serein.extensions import pipeline_pending as pending
    seed(settings, 'a', 1)

    save_settings(settings.database, {'pipeline': {'round_gate_enabled': True}})

    async def runner(role, request):
        return gated_output(role, request)

    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['processed_originals'] == 0
    assert p.new_batch(settings.database, True) is None
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    assert p.new_batch(settings.database, True, tomorrow) is None
    save_settings(settings.database, {'pipeline': {'max_input_chars': 50000}})
    assert p.new_batch(settings.database, True) is None
    with Store(settings.database, read_only=True) as store:
        item = pending.entries(store.conn)[0]
        assert item['status'] == 'open' and item['source_ids'] == [1, 2]
        before = [tuple(row) for row in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
    resumed = pending.resume(settings.database, item['id'], replan=True)
    batch = p.new_batch(settings.database, True)
    assert batch['id'] == resumed['batch_id']
    assert [m['id'] for m in json.loads(batch['input_json'])['messages']] == [1, 2]
    with Store(settings.database, read_only=True) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM raw_events ORDER BY id')] == before


def test_queue_upgrade_replays_only_known_published_snapshot_and_keeps_proofs(settings):
    seed(settings, 'a', 1)
    result = asyncio.run(p.advance(settings.database, include_recent=True,
                                  runner=lambda role, request: _output(role, request)))
    with Store(settings.database) as store:
        batch = dict(store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?', (result['batch_id'],)).fetchone())
        data = json.loads(batch['input_json'])
        data['runtime_revision'] = next(iter(recovery.QUEUE_COMPATIBLE_ROUTER_REVISIONS))
        batch['input_json'] = encode(data)
        # Downstream cache consumers have a checked snapshot and no Router jobs.
        store.conn.execute('DELETE FROM pipeline_jobs WHERE batch_id=?', (batch['id'],))
    assert recovery._frames(settings.database, batch)
    batch['status'] = 'pending'
    with pytest.raises(p.RoutingRecoveryError, match='incompatible frozen contract'):
        recovery._frames(settings.database, batch)
    batch['status'] = 'done'
    data['routing_result']['tracks'][0]['recent_source_message_ids'] = [999999]
    batch['input_json'] = encode(data)
    with pytest.raises(p.RoutingRecoveryError):
        recovery._frames(settings.database, batch)


async def _output(role, request):
    return output_for(role, request)


def test_backfilled_reply_cannot_complete_newer_live_questions():
    from serein.extensions.pipeline_limits import routing_blocks
    rows = [
        {'id': 1, 'role': 'user', 'content': 'Live question', 'created_at': '2026-11-06T16:00:00Z'},
        {'id': 2, 'role': 'user', 'content': 'Another live question', 'created_at': '2026-11-06T17:00:00Z'},
        {'id': 3, 'role': 'user', 'content': 'Historical question', 'created_at': '2026-10-31T07:00:00Z',
         'metadata': {'import_upload_id': 'synthetic-upload'}},
        {'id': 4, 'role': 'assistant', 'content': 'Historical reply', 'created_at': '2026-10-31T07:01:00Z',
         'metadata': {'import_upload_id': 'synthetic-upload'}},
    ]
    chunks = routing_blocks(rows)
    assert [[m['id'] for m in c] for c in chunks] == [[1, 2], [3, 4]]
    assert not p.dialogue_unit_is_complete(chunks[0])
    assert p.dialogue_unit_is_complete(chunks[1])


def test_new_exchange_does_not_silently_reopen_reserved_material(settings):
    from serein.extensions import pipeline_pending as pending
    seed(settings, 'a', 1)
    save_settings(settings.database, {'pipeline': {'round_gate_enabled': True}})

    async def runner(role, request):
        return gated_output(role, request)

    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['processed_originals'] == 0 and p.new_batch(settings.database, True) is None
    seed(settings, 'a', 1, rounds=2)
    batch = p.new_batch(settings.database, True)
    assert [m['id'] for m in json.loads(batch['input_json'])['messages']] == [5, 6]
    with Store(settings.database, read_only=True) as store:
        item = pending.entries(store.conn)[0]
        assert item['batch_id'] == result['batch_id']
        assert item['status'] == 'open' and item['source_ids'] == [1, 2]
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
