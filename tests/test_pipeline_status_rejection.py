"""Legacy preflight refusals remain auditable without hiding actual failures."""
import json

import pytest
from fastapi.testclient import TestClient

from test_public_features import settings
from serein.api.http import create_app
from serein.core.store import Store, encode
from serein.extensions import pipeline as p
from serein.work_tasks import status


@pytest.mark.parametrize('stage,job,error,rejected',[
    ('starting','','版本已变化，请选择重新切分，不复用旧执行计划',True),
    ('event_writer','','版本已变化，请选择重新切分，不复用旧执行计划',False),
    ('starting','real-job','版本已变化，请选择重新切分，不复用旧执行计划',False),
    ('starting','','上游返回 HTTP 503',False),
])
def test_status_distinguishes_legacy_request_refusal_from_failed_work(settings,stage,job,error,rejected):
    p.initialize(settings.database)
    previous=status(settings.database,'pipeline')
    previous.update(status='failed',stage=stage,job_id=job,error=error,lease_until=0)
    with Store(settings.database) as store:
        store.conn.execute('INSERT OR REPLACE INTO background_state(name,value_json) VALUES (?,?)',
                           ('work:pipeline',encode(previous)))
    client=TestClient(create_app(settings,token='synthetic',live=True))
    response=client.get('/v1/pipeline/status',headers={'Authorization':'Bearer synthetic'})
    assert response.status_code==200
    value=response.json()
    assert value['status']==('idle' if rejected else 'failed')
    assert value.get('last_request_rejection')==(error if rejected else None)
    assert value['error']==('' if rejected else error)
    with Store(settings.database,read_only=True) as store:
        saved=json.loads(store.conn.execute("SELECT value_json FROM background_state WHERE name='work:pipeline'").fetchone()[0])
    assert saved==previous
