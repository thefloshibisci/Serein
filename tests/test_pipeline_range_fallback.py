"""Real queue regression for a later settlement using a prefix of a Router frame."""
import asyncio
from copy import deepcopy
import json

import pytest
from test_public_features import settings, output_for, synthetic_runner
from test_pipeline_route_history import freeze, history, move_card
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store, encode
from serein.extensions import pipeline as p


def wider_producer(settings):
    raw_archive(settings).ingest([
        {'source_event_id':str(i),'session_id':'range','role':'user' if i%2 else 'assistant',
         'text':f'Synthetic book plan {i}', 'created_at':f'2025-01-01T00:0{i}:00Z'}
        for i in range(1,5)],source='test')
    batch,data=freeze(settings)
    routed=history(settings,data)
    early=deepcopy(data)
    early.update(messages=data['messages'][:2],routing_messages=data['routing_messages'][:2],parked=[])
    with Store(settings.database) as store:
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(early),batch['id']))
        jobs=[tuple(r) for r in store.conn.execute("SELECT * FROM pipeline_jobs WHERE batch_id='route:synthetic'")]
    move_card(settings,routed)
    return batch,early,jobs


def test_fresh_prefix_is_routed_without_future_prose_or_repeating_producer(settings):
    batch,early,jobs=wider_producer(settings)
    with pytest.raises(p.RouteCacheRangeMismatch):p.cached_route_result(settings.database,early)
    calls=[]
    async def runner(role,request):
        calls.append(role)
        assert 'LATER WINDOW' not in request['prompt']
        if role=='track_router':assert [m['id'] for m in request['messages']]==[1,2]
        return output_for(role,request)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert result['status']=='processed' and result['events']==1
    assert calls==['track_router','event_curator','event_writer']
    with Store(settings.database,read_only=True) as store:
        assert [r[0] for r in store.conn.execute('SELECT raw_id FROM raw_processing ORDER BY raw_id')]==[1,2]
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==4
        assert [tuple(r) for r in store.conn.execute("SELECT * FROM pipeline_jobs WHERE batch_id='route:synthetic'")]==jobs


@pytest.mark.parametrize('damage',['source_hash','route','missing_output','scope','missing_proof'])
def test_wider_damaged_producer_still_blocks_before_any_model_call(settings,damage):
    batch,data,_=wider_producer(settings)
    with Store(settings.database) as store:
        row=store.conn.execute("SELECT * FROM pipeline_jobs WHERE batch_id='route:synthetic'").fetchone()
        if damage=='source_hash':store.conn.execute("UPDATE raw_events SET event_hash='changed' WHERE id=4")
        elif damage=='route':
            for table in ('pipeline_routes','pipeline_route_provenance'):
                store.conn.execute("UPDATE "+table+" SET route_json=json_set(route_json,'$.primary_track_id','foreign-track') WHERE raw_id=1")
        elif damage=='missing_output':store.conn.execute('UPDATE pipeline_jobs SET output_json=NULL WHERE id=?',(row['id'],))
        elif damage=='scope':store.conn.execute("UPDATE pipeline_batches SET scope='foreign' WHERE id='route:synthetic'")
        else:
            request=json.loads(row['request_json']);request.pop('routing_frame')
            store.conn.execute('UPDATE pipeline_jobs SET request_json=? WHERE id=?',(encode(request),row['id']))
    async def forbidden(*args):pytest.fail('Damaged producer reached a model')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='needs_repair'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0


def test_frozen_components_cannot_silently_reroute(settings):
    batch,data,_=wider_producer(settings)
    data['components']=[]
    with Store(settings.database) as store:
        store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(data),batch['id']))
    async def forbidden(*args):pytest.fail('Frozen downstream plan was rerouted')
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=forbidden))['status']=='needs_repair'
