"""Frontend Narrative reads, sealed previews, saves, uploads, and Arc receipts."""

import json
import logging

from fastapi import APIRouter, Request, Query
from starlette.responses import JSONResponse

from ..compat.narratives import narrative_transaction, RevisionInbox, Uploads
from ..compat.events import Events, project_events
from ..compat.diaries import Diaries
from ..compat.scenes import Scenes
from ..compat.germany.fact_events import FactEventSettlementBlockedError
from ..compat.germany.narrative_http import NarrativeHTTP
from ..compat.germany.narrative_source_payload import materialize_sources
from ..compat.germany.narrative_materials import normalize_material_ids, render_material_snapshot
from ..core.store import now
from ..compat.narrative_review import review_revision


class ArcEvents(Events):
    def __init__(self, database, store):
        super().__init__(database)
        self.store = store

    def link_arc_events(self, arc_key, events):
        # NarrativeHTTP validated the bounded receipt list and current Arc CAS.
        # Its caller owns the write lock from validation through projection.
        entry = next((r for r in self.store.conn.execute("SELECT r.metadata_json FROM documents d JOIN revisions r ON r.document_id=d.id AND r.number=d.revision WHERE d.kind='narrative'")
                      if json.loads(r[0]).get('legacy_registry', {}).get('arc_key') == arc_key), None)
        excluded = set(json.loads(entry[0])['legacy_registry'].get('excluded_event_ids') or []) if entry else set()
        if any(item['event_id'] in excluded for item in events):
            raise FactEventSettlementBlockedError('Material was explicitly withdrawn from this Arc')
        inserted = 0
        for item in events:
            key, fingerprint = item['event_id'], item['fingerprint']
            existing = self.store.conn.execute(
                'SELECT event_fingerprint FROM fact_event_arc_links WHERE arc_key=? AND event_id=?',
                (arc_key, key)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise FactEventSettlementBlockedError('Arc link fingerprint drifted: ' + key)
                continue
            self.store.conn.execute('INSERT INTO fact_event_arc_links VALUES (?,?,?,?)',
                                     (arc_key, key, fingerprint, now()))
            inserted += 1
            self.store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)',(key,))
        project_events(self.store.conn)
        return {'status': 'updated' if inserted else 'idempotent', 'arc_key': arc_key,
                'inserted': inserted, 'idempotent': len(events) - inserted,
                'event_ids': [item['event_id'] for item in events], 'body_unchanged': True}


def endpoints(settings, rolls):
    api = NarrativeHTTP()
    api.rolls = rolls
    api.events = ArcEvents(settings.database, rolls.store)
    api.uploads = Uploads(rolls.store)
    api.inbox = RevisionInbox(rolls.store)
    api.materialize = lambda narrative: materialize_sources(
        narrative, api.events, Scenes(settings.database), Diaries(settings.database), api.uploads)
    return api


def routes(settings, auth):
    router = APIRouter(dependencies=auth)

    def install(path, method, name, write):
        async def endpoint(request: Request):
            # Materialize the request before taking a database write lock.
            if method != 'GET':
                await request.body()
            with narrative_transaction(settings.database, write=write) as rolls:
                result = await getattr(endpoints(settings, rolls), name)(request)
                if result.status_code >= 400:
                    rolls.store.conn.rollback()
                return result
        router.add_api_route(path, endpoint, methods=[method], name=name)

    for path, method, name, write in [
        ('/api/narrative-rolls', 'GET', 'api_narrative_rolls', False),
        ('/api/narrative-rolls/material-uploads', 'POST', 'api_narrative_material_upload', True),
        ('/api/narrative-rolls/preview-input', 'POST', 'api_narrative_roll_preview_input', False),
        ('/api/narrative-rolls/save-body', 'POST', 'api_save_narrative_roll_body', True),
        ('/api/narrative-arcs/cards', 'GET', 'api_narrative_arc_cards', False),
        ('/api/narrative-arcs/append-event-materials', 'POST', 'api_append_narrative_arc_event_materials', True),
    ]:
        install(path, method, name, write)

    @router.get('/api/narrative-revision-inbox')
    def inbox(status: str='pending', narrative_id: str='', limit: int=50):
        with narrative_transaction(settings.database) as rolls:
            return RevisionInbox(rolls.store).list(status=status, narrative_id=narrative_id, limit=limit)

    @router.get('/api/narrative-revision-inbox/{proposal_id}/materials')
    def revision_materials(proposal_id: str, offset: int=Query(0, ge=0), limit: int=Query(50, ge=1, le=100),
                           kind: str='', identifier: str=''):
        from ..compat.revision_materials import read_materials
        return read_materials(settings.database, proposal_id, offset=offset, limit=limit, kind=kind, identifier=identifier)

    @router.post('/api/narrative-rolls/discover-theme')
    async def discover_theme(body: dict):
        import httpx
        from ..compat.narrative_theme import discover, validate_theme
        def materialize(narrative):
            with narrative_transaction(settings.database) as rolls:
                return endpoints(settings, rolls).materialize(narrative)
        try:
            return await discover(settings, validate_theme(body.get('theme')), materialize)
        except ValueError as exc:
            return JSONResponse({'status': 'invalid', 'message': str(exc)}, status_code=400)
        except (httpx.TimeoutException, TimeoutError):
            return JSONResponse({'status': 'error', 'message': '选材模型响应超时，材料没有保存，请稍后重试。'}, status_code=504)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logging.getLogger(__name__).warning('Narrative theme provider returned HTTP %s', status)
            return JSONResponse({'status': 'error', 'message': f'选材模型暂不可用（上游 HTTP {status}），请稍后重试。'}, status_code=502)
        except Exception:
            # Provider errors can contain credentials or private material; keep them server-side.
            return JSONResponse({'status': 'error', 'message': '这次选材没有完成，请稍后重试。'}, status_code=502)

    @router.post('/api/narrative-rolls/create-line')
    def create_line(body: dict):
        from ..compat.narrative_theme import validate_creation, material_rows, source_receipt
        try:
            theme, title, key, ids, receipts = validate_creation(body)
            ids = normalize_material_ids(ids)
        except ValueError as exc:
            return JSONResponse({'status': 'invalid', 'message': str(exc)}, status_code=400)
        except (TypeError, AttributeError):
            return JSONResponse({'status': 'invalid', 'message': '选材数据格式无效，请重新找材料。'}, status_code=400)
        with narrative_transaction(settings.database, write=True) as rolls:
            document = rolls.store.read(key)
            if document and document['lifecycle'] == 'deleted':
                return JSONResponse({'status': 'conflict', 'reason': 'narrative_deleted',
                    'message': '这本叙事卷已经删除，请重新找材料后新建。'}, status_code=409)
            current = rolls.read(key)
            if current.get('status') == 'ok':
                same = (current.get('title') == title and current.get('current_status_cue') == '主题：' + theme
                        and all(set(map(str, current.get(f'linked_{kind}_ids') or [])) == set(map(str, ids[f'{kind}_ids']))
                                for kind in ('event', 'scene', 'diary', 'darkroom', 'upload')))
                return JSONResponse({'status': 'idempotent' if same else 'conflict', 'narrative_id': key,
                    **({'narrative': current} if same else {'reason': 'creation_request_changed',
                        'message': '这次创建请求已保存过另一份书名或材料，请重新找材料后新建。'})}, status_code=200 if same else 409)
            proposed = {'narrative_id': key, 'title': title,
                        **{f'linked_{kind}_ids': ids[f'{kind}_ids'] for kind in ('event', 'scene', 'diary')}}
            materials = endpoints(settings, rolls).materialize(proposed)
            if materials.get('status') != 'ok':
                return JSONResponse(materials, status_code=409)
            rows = material_rows(materials)
            if any(receipts.get((row['source_type'], row['source_id'])) != source_receipt(row) for row in rows):
                return JSONResponse({'status': 'conflict', 'message': '材料已变化，请重新找材料后保存。'}, status_code=409)
            dates = sorted(str(row['date'])[:10] for row in rows if row.get('date'))
            result = rolls.publish(narrative_id=key, expected_revision=0, title=title,
                document=f'# {title}\n\n## 第一人称叙事\n\n' + render_material_snapshot(materials),
                publication_status='collecting', arc_key='arc:' + key,
                current_status_cue='主题：' + theme, query_cues=[theme],
                time_start=dates[0] if dates else '', time_end=dates[-1] if dates else '',
                **{f'source_{kind}_ids': ids[f'{kind}_ids'] for kind in ('event', 'scene', 'diary')})
            if result['status'] == 'created':
                result['narrative'] = {**result, 'status': 'ok'}
            return JSONResponse(result, status_code=200 if result['status'] == 'created' else 409)

    @router.post('/api/narrative-rolls/rename')
    def rename_roll(body: dict):
        key, revision = body.get('narrative_id'), body.get('expected_revision')
        title = body.get('title')
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 16 or any(c in title for c in '\n\r#'):
            return JSONResponse({'status': 'invalid', 'message': '书名请填写 1–16 个字符，不要换行或包含 #。'}, status_code=400)
        if not isinstance(key, str) or not key or type(revision) is not int or revision < 1:
            return JSONResponse({'status': 'invalid', 'reason': 'invalid_expected_revision'}, status_code=400)
        with narrative_transaction(settings.database, write=True) as rolls:
            result = rolls.rename(key, revision, title.strip())
            return JSONResponse(result, status_code=404 if result['status'] == 'not_found'
                else 409 if result['status'] == 'conflict' else 200)

    @router.post('/api/narrative-rolls/delete')
    def delete_roll(body: dict):
        key = body.get('narrative_id')
        revision = body.get('expected_revision')
        if (body.get('confirm') != 'DELETE' or not isinstance(key, str) or not key
                or type(revision) is not int or revision < 1):
            return JSONResponse({'status': 'invalid', 'reason': 'invalid_delete_request'}, status_code=400)
        with narrative_transaction(settings.database, write=True) as rolls:
            result = rolls.delete(key, revision)
            return JSONResponse(result, status_code=200 if result['status'] == 'deleted'
                else 404 if result['status'] == 'not_found' else 409)

    @router.post('/api/narrative-rolls/save-materials')
    def save_materials(body: dict):
        with narrative_transaction(settings.database, write=True) as rolls:
            current = rolls.read(body.get('narrative_id', ''))
            if current.get('status') != 'ok' or current.get('publication_status') != 'collecting' or current.get('body', '').strip():
                return JSONResponse({'status': 'conflict', 'reason': 'requires_collecting_line'}, status_code=409)
            if current['revision'] != body.get('expected_revision'):
                return JSONResponse({'status': 'conflict', 'reason': 'narrative_revision_changed'}, status_code=409)
            try:
                ids = normalize_material_ids(body.get('material_ids'))
            except ValueError as exc:
                return JSONResponse({'status': 'invalid', 'reason': str(exc)}, status_code=400)
            proposed = {**current, **{f'linked_{kind}_ids': ids[f'{kind}_ids'] for kind in ('event','scene','diary','darkroom','upload')}}
            materials = endpoints(settings, rolls).materialize(proposed)
            if materials.get('status') != 'ok':
                return JSONResponse(materials, status_code=409)
            result = rolls.publish(narrative_id=current['narrative_id'], expected_revision=current['revision'],
                document=f"# {current['title']}\n\n## 第一人称叙事\n\n" + render_material_snapshot(materials),
                **{field: current.get(field) for field in ('title','arc_key','parent_narrative_id','title_aliases',
                    'primary_entities','supporting_entities','intent_tags','query_cues','time_start','time_end',
                    'current_status_cue','lifecycle')}, publication_status='collecting',
                **{f'source_{kind}_ids': ids[f'{kind}_ids'] for kind in ('event','scene','diary','darkroom','upload')})
            return JSONResponse(result, status_code=200 if result['status']=='updated' else 409)

    @router.patch('/api/narrative-revision-inbox/{proposal_id}')
    def review(proposal_id: str, body: dict):
        with narrative_transaction(settings.database, write=True) as rolls:
            result = review_revision(rolls, proposal_id,
                action=body.get('action', ''), draft_delta=body.get('draft_delta', ''), note=body.get('note', ''))
            return JSONResponse(result, status_code=404 if result['status']=='not_found' else 409 if result['status']=='conflict' else 400 if result['status']=='invalid' else 200)

    @router.post('/api/narrative-revision-inbox/scan')
    async def scan(body: dict):
        from ..compat.scout import Scout
        return await Scout(settings)._scan_narrative_revision_inbox()

    return router
