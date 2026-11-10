"""Automatic settlement must reconsider late old sources after a quiet day."""
import asyncio

from test_public_features import settings, output_for
from test_route_scope_holds import Clock, seed, api, reset_clock
from serein.core.store import Store
from serein.extensions import pipeline as p


def test_late_old_upload_reopens_completed_day_without_manual_next(settings,monkeypatch):
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='current'
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='settled_today'
    assert calls==[]
    seed(settings,start=Clock.value.replace(hour=1,minute=0),count=1)
    result=asyncio.run(p.scheduled_advance(settings.database))
    assert result['status']=='processed' and result['events']==1 and result['processed_originals']==2
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT completed FROM pipeline_schedule').fetchone()[0]==0
    count=len(calls)
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='current'
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='settled_today'
    assert len(calls)==count


def test_today_sources_do_not_bypass_settlement_watermark(settings,monkeypatch):
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='current'
    seed(settings,start=Clock.value.replace(hour=3,minute=5),count=1)
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='settled_today'
    assert calls==[]
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_explicit_pending_plan_runs_after_day_was_completed(settings,monkeypatch):
    calls=api(settings,monkeypatch,lambda r:output_for(r['role'],r))
    assert asyncio.run(p.scheduled_advance(settings.database))['status']=='current'
    seed(settings,start=Clock.value.replace(hour=1,minute=0),count=1)
    batch=p.new_batch(settings.database,False)
    assert batch is not None
    result=asyncio.run(p.scheduled_advance(settings.database))
    assert result['batch_id']==batch['id'] and result['events']==1
