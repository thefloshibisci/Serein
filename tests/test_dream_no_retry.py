"""Synthetic checks for bounded dream retries and durable draft recovery."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from serein.compat.dreams import Dreams
from serein.compat.germany.dream_engine import DreamEngine, EmptyDreamResponse
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


@pytest.mark.parametrize('failure',[TimeoutError, asyncio.CancelledError])
def test_failed_dream_stays_stopped_after_restart_but_next_day_is_new(live,failure):
    settings,_=live
    engine=ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=engine.tz)
    engine._call_dream_model.side_effect=failure('Synthetic request failure')
    with pytest.raises(failure): asyncio.run(engine.generate(None,now=stamp))
    assert engine._call_dream_model.await_count==1
    for worker in (engine,ready(settings)):
        result=asyncio.run(worker.generate(None,now=stamp+timedelta(hours=1)))
        assert result['reason']=='daily_attempt_already_started'
    restarted=ready(settings)
    result=asyncio.run(restarted.generate(None,now=stamp+timedelta(days=1)))
    assert result['status']=='created'
    assert restarted._call_dream_model.await_count==1
    assert asyncio.run(restarted.generate(None,now=stamp+timedelta(days=1,hours=1)))['status']=='exists'
    assert restarted._call_dream_model.await_count==1

@pytest.mark.parametrize('failure',[EmptyDreamResponse('empty'), UpstreamError(httpx.Response(429))])
@pytest.mark.parametrize('retry_succeeds',[True, False])
def test_dream_retries_once_after_restart_without_redrawing(live, monkeypatch, failure, retry_succeeds):
    settings,_=live
    engine=ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=engine.tz)
    engine._call_dream_model.side_effect=failure
    with pytest.raises(type(failure)):
        asyncio.run(engine.generate(None,now=stamp))
    restarted=ready(settings)
    def unexpected_roll():
        pytest.fail('An already selected dream must not roll again')
    monkeypatch.setattr('serein.compat.germany.dream_engine.random.random',unexpected_roll)
    if retry_succeeds:
        assert asyncio.run(restarted.run_due(None,now=stamp+timedelta(hours=1)))['status']=='created'
        assert len(restarted.list_records())==1
    else:
        restarted._call_dream_model.side_effect=failure
        with pytest.raises(type(failure)):
            asyncio.run(restarted.run_due(None,now=stamp+timedelta(hours=1)))
        again=ready(settings)
        assert asyncio.run(again.run_due(None,now=stamp+timedelta(hours=2)))['reason']=='daily_attempt_already_started'
        assert again._call_dream_model.await_count==0
    assert restarted._call_dream_model.await_count==1


def test_dream_draft_survives_failed_publication_and_restart(live):
    settings,_=live
    engine=ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=engine.tz)
    log_event=engine.log_event
    def fail_after_save(store, event, payload):
        if event=='generated':
            raise OSError('Synthetic publication failure after inserting dream')
        log_event(store,event,payload)
    engine.log_event=fail_after_save
    with pytest.raises(OSError):
        asyncio.run(engine.generate(None,now=stamp))
    assert not engine.list_records()
    restarted=ready(settings)
    restarted.client=None
    assert asyncio.run(restarted.run_due(None,now=stamp+timedelta(hours=5)))['status']=='created'
    assert [r.body for r in restarted.list_records()]==['Synthetic dream text.']
    assert restarted._call_dream_model.await_count==0
    with Store(settings.database,read_only=True) as store:
        assert not store.conn.execute("SELECT 1 FROM background_state WHERE name GLOB 'dream-draft:*'").fetchone()
        assert store.conn.execute("SELECT count(*) FROM historical_work_events WHERE event='generated'").fetchone()[0]==1



def test_competing_dream_workers_cannot_issue_two_requests(live):
    settings,_=live
    first,second=ready(settings),ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=first.tz)
    async def run():
        both=asyncio.Event(); arrivals=0
        async def materials(*args):
            nonlocal arrivals
            arrivals+=1
            if arrivals==2: both.set()
            await both.wait()
            rows=[{'id':'synthetic','content':'Synthetic material','metadata':{}}]*5
            return rows
        first.select_materials=second.select_materials=materials
        return await asyncio.gather(first.generate(None,now=stamp),second.generate(None,now=stamp))
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
