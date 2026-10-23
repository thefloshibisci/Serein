"""Synthetic transport and persisted timeout diagnostics; no real model calls."""
import asyncio
import json
import math

import httpx
import pytest

from serein import model_runtime
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.work_tasks import failure_reason
from test_public_features import settings, ingest


@pytest.mark.parametrize('error_type,category',[
    (httpx.ReadTimeout,'read_timeout'),(httpx.ConnectTimeout,'connect_timeout'),
    (httpx.WriteTimeout,'write_timeout'),(httpx.PoolTimeout,'pool_timeout'),
    (httpx.TimeoutException,'transport_timeout'),(TimeoutError,'deadline_timeout'),
])
def test_timeout_categories_are_distinct_and_private_messages_are_omitted(error_type,category):
    error=error_type('PRIVATE_BODY api-key=PRIVATE_KEY https://user:PRIVATE_PASSWORD@example.invalid/path?secret=PRIVATE_QUERY')
    reason=failure_reason(error)
    assert f'[{category}]' in reason
    assert 'PRIVATE' not in reason and 'example.invalid' not in reason


def fake_client(monkeypatch, *, wait=False):
    class Client:
        def __init__(self,**kwargs):self.timeout=kwargs['timeout']
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return None
        async def post(self,*args,**kwargs):
            if wait:await asyncio.Event().wait()
            raise httpx.ReadTimeout('PRIVATE_PROVIDER_RESPONSE')
    monkeypatch.setattr(httpx,'AsyncClient',Client)


def test_transport_timeout_records_effective_limits_and_elapsed_without_input(monkeypatch):
    fake_client(monkeypatch)
    model={'model':'PRIVATE_MODEL','api_key':'PRIVATE_KEY',
           'base_url':'https://user:PRIVATE_PASSWORD@example.invalid/private?token=PRIVATE_QUERY',
           'request_timeout_seconds':73}
    with pytest.raises(httpx.ReadTimeout) as caught:
        asyncio.run(model_runtime.complete(model,{'messages':[{'role':'user','content':'PRIVATE_BODY'}]}))
    reason=failure_reason(caught.value)
    assert 'read_timeout_seconds=73' in reason
    assert 'connect_timeout_seconds=15' in reason and 'pool_timeout_seconds=15' in reason
    assert 'elapsed_seconds=' in reason
    assert 'PRIVATE' not in reason and 'example.invalid' not in reason


def test_outer_deadline_keeps_cancelled_transport_timing_without_becoming_read_timeout(monkeypatch):
    fake_client(monkeypatch,wait=True)
    async def invoke():
        await asyncio.wait_for(model_runtime.complete(
            {'model':'synthetic','base_url':'https://synthetic.invalid/v1','request_timeout_seconds':73},
            {'messages':[{'role':'user','content':'PRIVATE_BODY'}]}),timeout=.01)
    with pytest.raises(TimeoutError) as caught:asyncio.run(invoke())
    reason=failure_reason(caught.value)
    assert '[deadline_timeout]' in reason and '[read_timeout]' not in reason
    assert 'read_timeout_seconds=73' in reason and 'elapsed_seconds=' in reason
    assert 'PRIVATE' not in reason


def test_timeout_metadata_whitelists_finite_numbers_only():
    error=httpx.ReadTimeout('PRIVATE_ERROR')
    error._serein_timeout_diagnostics={'elapsed_seconds':'PRIVATE_BODY','read_timeout_seconds':math.inf,
        'connect_timeout_seconds':15,'pool_timeout_seconds':True,'api_key':'PRIVATE_KEY',
        'url':'https://user:PRIVATE_PASSWORD@example.invalid/?secret=PRIVATE_QUERY'}
    reason=failure_reason(error)
    assert 'connect_timeout_seconds=15' in reason
    assert 'PRIVATE' not in reason and 'example.invalid' not in reason
    assert 'elapsed_seconds=' not in reason and 'read_timeout_seconds=' not in reason and 'pool_timeout_seconds=' not in reason


def test_pipeline_persists_diagnostics_and_preserves_three_run_failure_budget(settings,monkeypatch):
    ingest(settings)
    save_settings(settings.database,{
        'models':[{'id':'synthetic','model':'synthetic','api_key':'synthetic-not-a-secret','base_url':'https://synthetic.invalid/v1'}],
        'assignments':{role:'synthetic' for role in p.ROLES},'pipeline':{'execution_mode':'api'}})
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return None
        async def post(self,*args,**kwargs):
            calls.append(1);raise httpx.ReadTimeout('PRIVATE_PROVIDER_RESPONSE')
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    for number in (1,2):
        with pytest.raises(httpx.ReadTimeout):asyncio.run(p.advance(settings.database,include_recent=True))
        assert len(calls)==number
    paused=asyncio.run(p.advance(settings.database,include_recent=True))
    assert paused['status']=='paused' and paused['failures']==3 and len(calls)==3
    with Store(settings.database,read_only=True) as store:
        attempts=[dict(row) for row in store.conn.execute('SELECT * FROM pipeline_attempts ORDER BY attempt')]
        assert len(attempts)==3
        assert all('[read_timeout]' in row['error'] and 'elapsed_seconds=' in row['error'] for row in attempts)
        assert all('read_timeout_seconds=600' in row['error'] for row in attempts)
        assert 'PRIVATE' not in json.dumps(attempts)
