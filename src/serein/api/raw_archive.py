"""Raw dialogue ingestion and lookup used by the existing Bridge queue."""

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..compat.raw_archive import raw_archive
from ..compat.germany.raw_ingest import _raw_ingest_events_from_body
from ..core.store import Conflict


def routes(settings, auth):
    router = APIRouter(dependencies=auth)
    archive = raw_archive(settings)
    from ..source_management import SourceManagement
    from ..compat.originals import Originals
    manager = SourceManagement(settings)

    @router.post('/v1/originals/search')
    def originals_search(body: dict):
        return Originals(settings.database).source_message_search(**body)

    @router.post('/v1/originals/read')
    def originals_read(body: dict):
        return Originals(settings.database).source_message_read(**body)

    @router.post('/v1/originals/upload')
    def originals_upload(body: dict):
        try: return manager.upload(**body)
        except Conflict as error: raise HTTPException(409,str(error)) from None

    @router.post('/v1/originals/delete-preview')
    def originals_delete_preview(body: dict):
        return manager.preview_delete(body.get('ids'))

    @router.post('/v1/originals/delete')
    def originals_delete(body: dict):
        try: return manager.delete(body.get('items'),body.get('confirm'))
        except Conflict as error: raise HTTPException(409,str(error)) from None

    @router.post('/api/ingest-raw')
    def ingest(body: dict, request: Request, background_tasks: BackgroundTasks):
        session = request.headers.get('X-Serein-Session-Id', '').strip()
        events = _raw_ingest_events_from_body(body, default_session_id=session, default_conversation_id=session)
        if not events:
            raise HTTPException(400, 'missing events')
        if len(events)>archive.max_ingest_batch:
            raise HTTPException(413, f'At most {archive.max_ingest_batch} original messages per request; split larger imports')
        result = archive.ingest(events, source=str(body.get('source') or 'raw'))
        inserted = [item['id'] for item in result['items']
                    if item['status'] == 'inserted' and item['source'] == 'orangechat' and item['role'] == 'assistant']
        if inserted:
            from ..host_persona import evaluate_uploaded
            background_tasks.add_task(evaluate_uploaded, settings.database, inserted)
        return result

    @router.api_route('/api/search-raw', methods=['GET', 'POST'])
    async def search(request: Request):
        params = dict(request.query_params)
        if request.method == 'POST':
            params.update(await request.json())
        return archive.search(query=str(params.get('q', params.get('query', '')) or ''),
            limit=int(params.get('limit') or 10),
            **{k: str(params.get(k) or '') for k in ('source', 'role', 'conversation_id', 'session_id', 'since', 'until')})

    return router
