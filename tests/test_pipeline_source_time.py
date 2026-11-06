"""Legacy mobile timestamps must not stop otherwise valid dialogue processing."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from test_public_features import settings, raw_archive, synthetic_runner
from serein.core.store import Store
from serein.extensions import pipeline as p
from serein.extensions.pipeline_rules import flushable_dialogue_units
from serein.extensions.pipeline_limits import settlement_window, blocks
from serein.source_time import source_datetime, source_julianday
from serein.extensions import pipeline_latest as latest


def originals():
    return [
        dict(id=1, source='orangechat', source_event_id='u', session_id='mobile', role='user',
             created_at='2026-11-06T02:35:00', text='Synthetic question'),
        dict(id=2, source='orangechat', source_event_id='a', session_id='mobile', role='assistant',
             created_at='2026-11-05T18:36:00Z', text='Synthetic answer'),
    ]


def test_mixed_mobile_timestamps_share_the_same_silence_and_settlement_clock():
    rows = originals()
    before = [dict(row) for row in rows]
    end = datetime(2026, 11, 5, 18, 36, tzinfo=timezone.utc)
    assert flushable_dialogue_units(rows, now=end+timedelta(minutes=19)) == []
    assert flushable_dialogue_units(rows, now=end+timedelta(minutes=20)) == [rows]
    assert settlement_window(rows).isoformat() == '2026-11-06T03:00:00+08:00'
    assert blocks(rows) == [rows]
    assert rows == before


def test_mobile_originals_can_settle_without_rewriting_the_raw_archive(settings):
    rows = originals()
    archive = raw_archive(settings)
    archive.ingest([{k:v for k,v in row.items() if k!='id'} for row in rows], source='orangechat')
    p.initialize(settings.database)
    frozen = p.new_batch(settings.database, True, clock=datetime(2026,11,6,3,tzinfo=timezone(timedelta(hours=8))))
    assert frozen is not None
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))
    assert result['status'] == 'processed'
    assert result['events'] == 1
    with Store(settings.database, read_only=True) as store:
        assert [r[0] for r in store.conn.execute('SELECT created_at FROM raw_events ORDER BY id')] == [r['created_at'] for r in rows]
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=synthetic_runner))['status'] == 'current'


@pytest.mark.parametrize('value,source,expected', [
    ('2026-11-06T02:35:00', 'orangechat', '2026-11-05T18:35:00+00:00'),
    ('2026-11-06T02:35:00', 'bridge', '2026-11-06T02:35:00+00:00'),
    ('2026-11-06T02:35:00+08:00', 'orangechat', '2026-11-05T18:35:00+00:00'),
    ('2026-11-06T02:35:00-05:00', 'orangechat', '2026-11-06T07:35:00+00:00'),
    ('2026-11-05T18:35:00.123456Z', 'orangechat', '2026-11-05T18:35:00.123456+00:00'),
])
def test_source_clock_matches_sql_without_changing_explicit_offsets(value, source, expected):
    assert source_datetime(value, source).isoformat() == expected
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE raw_events(created_at TEXT,source TEXT)')
        db.execute('INSERT INTO raw_events VALUES (?,?)', (value, source))
        actual = db.execute(f'SELECT {source_julianday("r")} FROM raw_events r').fetchone()[0]
        wanted = db.execute('SELECT julianday(?)', (expected,)).fetchone()[0]
        assert actual == pytest.approx(wanted, abs=1e-8, rel=0)


def test_curator_orders_mixed_source_clocks_and_writer_keeps_local_time():
    rows = [dict(row, session_id=1, content=row['text']) for row in originals()]
    component = {'messages': rows, 'context_messages': rows, 'memberships': [
        {'unit_root_message_id': row['id'], 'source_message_ids': [row['id']], 'track_id': 'track'}
        for row in rows
    ]}
    assert [unit['root'] for unit in latest.event_curator_model_input(component)['units']] == [1, 2]
    assert [row['created_at'] for row in latest.writer_transcript_payload(rows)] == [
        '2026-11-06T02:35:00+08:00', '2026-11-06T02:36:00+08:00']
    from serein.compat.germany.fact_events import _source_bounds
    bounds = _source_bounds([dict(row, source_system=row['source']) for row in rows])
    assert bounds['local_date'] == '2026-11-06'
    assert bounds['local_start_time'] == '02:35' and bounds['local_end_time'] == '02:36'


def test_optional_candidate_window_handles_legacy_mobile_card_clocks():
    from serein.extensions.pipeline_track_candidates import select
    cards = [{'track_id': 'old', 'recent_turns': [{'created_at': '2026-11-05T08:00:00'}]},
             {'track_id': 'recent', 'recent_turns': [{'created_at': '2026-11-05T14:36:00'}]}]
    assert [card['track_id'] for card in select(cards, originals()[1:], [], {'track_candidates_enabled': True})] == ['recent']


def test_recent_originals_and_track_guards_compare_actual_instants(settings):
    rows = originals()
    raw_archive(settings).ingest([{k:v for k,v in row.items() if k!='id'} for row in rows], source='orangechat')
    p.initialize(settings.database)
    with Store(settings.database) as store:
        stored = [p.task_message(row) for row in store.conn.execute('SELECT * FROM raw_events ORDER BY id')]
        assert [row['id'] for row in p.recent_originals(store, 'orangechat', 'mobile', stored[1])] == [stored[0]['id']]
        assert p.router_recent(stored[:1], stored[1:]) == stored[:1]
        p.track_state.assert_before(store, [{'track_id': 'track', 'recent_source_message_ids': [stored[0]['id']]}], stored[1]['id'])
        store.conn.execute('UPDATE raw_events SET created_at=? WHERE id=?', ('2026-11-06T03:00:00', stored[0]['id']))
        with pytest.raises(p.RoutingRecoveryError, match='future'):
            p.track_state.assert_before(store, [{'track_id': 'track', 'recent_source_message_ids': [stored[0]['id']]}], stored[1]['id'])


def test_daytime_mobile_routing_retries_do_not_duplicate_or_consume_originals(settings, monkeypatch):
    from serein.deployment import save_settings
    from test_public_features import output_for
    rows = []
    for index in range(5):
        for row in originals():
            rows.append({**{k:v for k,v in row.items() if k!='id'}, 'source_event_id': f'{index}-{row["source_event_id"]}',
                         'created_at': f'2025-01-01T10:{index * 2:02}:00' if row['role']=='user'
                         else f'2025-01-01T02:{index * 2 + 1:02}:00Z'})
    raw_archive(settings).ingest(rows, source='orangechat')
    save_settings(settings.database, {'models': [{'id': 'local', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:9/v1'}],
                                      'assignments': {'track_router': 'local'}})
    calls = []
    async def complete(model, payload):
        with Store(settings.database, read_only=True) as store:
            request = json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        calls.append(request['role'])
        assert request['role'] == 'track_router'
        return {'choices': [{'message': {'content': json.dumps(output_for('track_router', request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    asyncio.run(p.flush_routes(settings.database))
    first_calls = list(calls)
    assert first_calls
    asyncio.run(p.flush_routes(settings.database))
    assert calls == first_calls
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM pipeline_routes').fetchone()[0] == 10
        assert store.conn.execute('SELECT COUNT(*) FROM raw_processing').fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE kind='event'").fetchone()[0] == 0
        assert [r[0] for r in store.conn.execute('SELECT created_at FROM raw_events ORDER BY id')] == [r['created_at'] for r in rows]
