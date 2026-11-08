"""Pipeline image rescue never treats unavailable pixels as textual evidence."""
import asyncio
import json

import httpx
import pytest

from test_public_features import settings,output_for
from test_image_transcription import pictures
from serein.compat.raw_archive import raw_archive
from serein.deployment import save_settings
from serein.core.store import Store
from serein.extensions import pipeline as p


def seed(settings):
    archive=raw_archive(settings)
    archive.ingest([{'source_event_id':'images','session_id':'pictures','role':'user','text':'Look at these pictures',
        'created_at':'2025-01-01T00:00:00Z','metadata':{'attachments':[{'kind':'image','url':url} for url in pictures(3)]}},
        {'source_event_id':'reply','session_id':'pictures','role':'assistant','text':'I will look',
         'created_at':'2025-01-01T00:01:00Z'}],source='synthetic')
    archive.update_image_transcription(1,'failed',{'error':'synthetic async failure'})
    return archive


def test_async_failure_uses_router_not_the_failed_image_model(settings):
    archive=seed(settings)
    save_settings(settings.database,{'models':[{'id':'eyes','model':'eyes','base_url':'https://synthetic.invalid/v1'}],
                                     'assignments':{'image_transcription':'eyes'}})
    seen=[]
    async def runner(role,request):
        if request.get('transcription_only'):
            seen.append(request['execution']['task'])
            return {'image_transcriptions':[{'input_image':1,'text':'[画面] Synthetic lamp','unreadable':False}]}
        if role=='event_writer':assert not request['images'] and len(request['curator_image_transcriptions'])==3
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))['events']==1
    assert seen==['track_router']*3
    assert archive.get_event(1)['image_transcription_status']=='complete'


def test_router_without_vision_is_called_once_then_uses_count(settings):
    archive=seed(settings);seen=[]
    async def runner(role,request):
        if request.get('transcription_only'):
            seen.append(request['execution']['task'])
            raise httpx.HTTPStatusError('synthetic unsupported image',request=httpx.Request('POST','https://synthetic.invalid'),response=httpx.Response(400))
        if role in ('event_curator','event_writer'):
            assert request['images']==[] and request['curator_image_transcriptions']==[]
            assert request['image_fallbacks'][0]['text']=='user 发送了 3 张图'
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))['events']==1
    assert seen==['track_router']
    record=archive.get_event(1)['image_transcription']
    assert record['status']=='failed' and record['fallback_mode']=='presence_only' and record['items']==[]
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_image_holds').fetchone()[0]==0


def test_routing_model_api_receives_images_and_writer_never_does(settings,monkeypatch):
    seed(settings)
    save_settings(settings.database,{'models':[{'id':'route','model':'routing-model','api_key':'synthetic','base_url':'https://synthetic.invalid/v1'},
                                             {'id':'other','model':'other-model','api_key':'synthetic','base_url':'https://synthetic.invalid/v1'}],
        'assignments':{'track_router':'route','image_transcription':'other','event_curator':'other','event_writer':'other'},
        'pipeline':{'execution_mode':'api'}})
    seen=[]
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        if request.get('transcription_only'):
            seen.append(model['model']);assert isinstance(payload['messages'][1]['content'],list)
            output={'image_transcriptions':[{'input_image':1,'text':'Synthetic picture','unreadable':False}]}
        else:
            if request['role']=='event_writer':assert isinstance(payload['messages'][1]['content'],str)
            output=output_for(request['role'],request)
        return {'choices':[{'message':{'content':json.dumps(output)}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1
    assert seen==['routing-model']*3
