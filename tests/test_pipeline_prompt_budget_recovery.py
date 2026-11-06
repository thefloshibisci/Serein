import asyncio
import json

from test_public_features import settings, output_for
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p


def test_daytime_prompt_budget_pause_preserves_prefix_and_resumes(settings, monkeypatch):
    rows = []
    for n in range(5):
        for offset, role in enumerate(('user', 'assistant')):
            rows.append({'source_event_id': str(n * 2 + offset), 'session_id': 'budget-recovery',
                         'role': role, 'text': 'Synthetic dialogue',
                         'created_at': f'2025-01-01T{n:02}:{offset:02}:00Z'})
    raw_archive(settings).ingest(rows, source='test')
    save_settings(settings.database, {'models': [{'id': 'local', 'model': 'synthetic',
        'base_url': 'http://127.0.0.1:9/v1'}], 'assignments': {'track_router': 'local'},
        'pipeline': {'max_prompt_chars': 40000}})
    original_request = p.request_for
    calls = []

    def request_for(database, batch, role, **kwargs):
        request = original_request(database, batch, role, **kwargs)
        if role == 'track_router' and request['messages'][0]['id'] == 3:
            request['prompt'] = 'x' * 45000
        return request

    async def complete(model, payload):
        with Store(settings.database, read_only=True) as store:
            row = store.conn.execute('SELECT id,request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()
            request = json.loads(row['request_json'])
        calls.append(row['id'])
        return {'choices': [{'message': {'content': json.dumps(output_for('track_router', request))}}]}

    monkeypatch.setattr(p, 'request_for', request_for)
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database, read_only=True) as store:
        batch = dict(store.conn.execute('SELECT * FROM pipeline_batches').fetchone())
        assert batch['status'] == 'paused_failure'
        detail = json.loads(batch['result_json'])
        assert detail['repair_kind'] == 'prompt_budget'
        assert '40000' in detail['reason']
        accepted = dict(store.conn.execute('SELECT * FROM pipeline_jobs WHERE output_json IS NOT NULL').fetchone())
        assert store.conn.execute('SELECT count(*) FROM pipeline_attempts').fetchone()[0] == 1
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0] == 0
    assert len(calls) == 1
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls) == 1
    save_settings(settings.database, {'pipeline': {'max_prompt_chars': 200000}})
    p.retry_batch(settings.database, batch['id'])
    asyncio.run(p.flush_routes(settings.database))
    assert len(calls) == 5 and calls.count(accepted['id']) == 1
    with Store(settings.database, read_only=True) as store:
        assert dict(store.conn.execute('SELECT * FROM pipeline_jobs WHERE id=?', (accepted['id'],)).fetchone()) == accepted
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?', (batch['id'],)).fetchone()[0] == 'routed'
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes').fetchone()[0] == 10
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
