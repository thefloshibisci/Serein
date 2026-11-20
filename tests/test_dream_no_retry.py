"""Synthetic checks for bounded dream retries and durable draft recovery."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from serein.compat.dreams import Dreams
from serein.compat.germany.dream_engine import DreamEngine
from serein.core.store import Store
from serein.model_runtime import UpstreamError
from test_live_clients import live


def ready(settings):
    engine=Dreams(settings)
    engine.client=object()
    engine.enabled=engine.auto_enabled=True
    engine.daily_probability=1
    materials=[{'id':'synthetic','content':'Synthetic material','metadata':{}}]*5
    engine.select_materials=AsyncMock(return_value=materials)
    engine._payload_for=lambda *args: {'synthetic': True}
    engine._call_dream_model=AsyncMock(return_value='Synthetic dream text.')
    return engine


@pytest.mark.parametrize('failure',[TimeoutError(), ValueError('empty'), UpstreamError(httpx.Response(429))])
def test_failed_request_retries_immediately_and_saves_dream(live,failure):
    settings,_=live
    engine=ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=engine.tz)
    engine._call_dream_model.side_effect=[failure,'Synthetic dream text.']
    result=asyncio.run(engine.generate(None,now=stamp))
    assert result['status']=='created'
    assert engine._call_dream_model.await_count==2
    assert len(engine.list_records())==1
    assert asyncio.run(engine.run_due(None,now=stamp+timedelta(hours=1)))['status']=='exists'
    assert engine._call_dream_model.await_count==2


@pytest.mark.parametrize('failure',[TimeoutError(), ValueError('empty'), UpstreamError(httpx.Response(429))])
def test_second_failure_stops_until_next_day(live,failure):
    settings,_=live
    engine=ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=engine.tz)
    engine._call_dream_model.side_effect=failure
    with pytest.raises(type(failure)):
        asyncio.run(engine.generate(None,now=stamp))
    assert engine._call_dream_model.await_count==2
    restarted=ready(settings)
    assert asyncio.run(restarted.run_due(None,now=stamp+timedelta(hours=1)))['reason']=='daily_attempt_already_started'
    assert restarted._call_dream_model.await_count==0
    next_day=asyncio.run(restarted.generate(None,now=stamp+timedelta(days=1),force=True))
    assert next_day['status']=='created'
    assert restarted._call_dream_model.await_count==1


def test_interrupted_request_retries_after_restart(live):
    settings,_=live
    engine=ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=engine.tz)
    engine._call_dream_model.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine.generate(None,now=stamp))
    restarted=ready(settings)
    assert asyncio.run(restarted.run_due(None,now=stamp+timedelta(hours=1)))['status']=='created'
    assert restarted._call_dream_model.await_count==1
    assert len(restarted.list_records())==1


def test_dream_draft_is_published_without_calling_model_again(live):
    settings,_=live
    engine=ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=engine.tz)
    log_event=engine.log_event
    failed=[]
    def fail_after_save(store, event, payload):
        if event=='generated' and not failed:
            failed.append(True)
            raise OSError('Synthetic publication failure after inserting dream')
        log_event(store,event,payload)
    engine.log_event=fail_after_save
    result=asyncio.run(engine.generate(None,now=stamp))
    assert result['status']=='created'
    assert [r.body for r in engine.list_records()]==['Synthetic dream text.']
    assert engine._call_dream_model.await_count==1
    restarted=ready(settings)
    assert len(restarted.list_records())==1
    with Store(settings.database,read_only=True) as store:
        assert not store.conn.execute("SELECT 1 FROM background_state WHERE name GLOB 'dream-draft:*'").fetchone()
        assert store.conn.execute("SELECT count(*) FROM historical_work_events WHERE event='generated'").fetchone()[0]==1



def test_competing_dream_workers_cannot_issue_two_requests(live):
    settings,_=live
    first,second=ready(settings),ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=first.tz)
    async def run():
        entered=asyncio.Event(); release=asyncio.Event()
        async def model(*args):
            entered.set()
            await release.wait()
            return 'Synthetic dream text.'
        first._call_dream_model.side_effect=model
        running=asyncio.create_task(first.generate(None,now=stamp))
        await asyncio.wait_for(entered.wait(), timeout=3)
        try:
            competing=await second.generate(None,now=stamp)
        finally:
            release.set()
        return await running, competing
    results=asyncio.run(run())
    assert sum(result['status']=='created' for result in results)==1
    assert first._call_dream_model.await_count+second._call_dream_model.await_count==1


def test_dream_sdk_does_not_retry_http_errors(monkeypatch):
    import openai
    for name in ('SEREIN_DREAM','OMBRE_DREAM'):
        for suffix in ('ENABLED','API_KEY','MODEL','BASE_URL'):
            monkeypatch.delenv(name+'_'+suffix,raising=False)
    requests=[]
    def response(request):
        requests.append(request)
        return httpx.Response(503,json={'error':{'message':'Synthetic unavailable'}})
    original=openai.AsyncOpenAI
    monkeypatch.setattr(openai,'AsyncOpenAI',lambda **kwargs: original(
        **kwargs,http_client=httpx.AsyncClient(transport=httpx.MockTransport(response))))
    engine=DreamEngine({'dream':{'enabled':True,'api_key':'synthetic',
        'base_url':'https://synthetic.invalid/v1','model':'synthetic'}})
    engine._dream_prompt=lambda:'Synthetic prompt'
    async def run():
        try:
            with pytest.raises(openai.APIStatusError): await engine._call_dream_model({})
        finally: await engine.client.close()
    asyncio.run(run())
    assert len(requests)==1
