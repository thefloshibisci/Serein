"""Scene API and backend tool calls without registering an assistant MCP."""

from ..core.domains import canonical_domain

import json
import re
import asyncio
from fastapi import APIRouter, HTTPException, Query

from ..compat.scenes import Scenes, scene_payload
from ..compat.events import Events
from ..core.reader import Reader
from ..core.store import digest,encode
from ..deployment import identity


def routes(settings,services,auth):
    router=APIRouter(dependencies=auth)
    scenes=Scenes(settings.database)

    @router.post('/api/scenes/find-by-source-keys')
    def source_scenes(body: dict):
        from ..core.source_scenes import find_source_scenes
        return find_source_scenes(settings.database, body.get('source_keys'))

    @router.get('/api/handoff-scenes')
    def handoff(limit: int=Query(500,ge=1,le=1000)):
        return scenes.handoff(limit)

    @router.get('/api/buckets/light')
    def light(include_archive: bool=False,limit: int=Query(500,ge=1,le=2000),offset: int=Query(0,ge=0)):
        with Reader(settings.database) as reader:
            items=[]
            for row in reader.store.conn.execute("SELECT id FROM documents WHERE kind='scene' AND lifecycle!='deleted'"):
                obj=reader.read(row[0],with_evidence=False)
                if not obj['readable']:continue
                if not include_archive and obj['status']!='active':continue
                doc=obj['document'];items.append({**scene_payload(doc)['metadata'],'id':row[0],'name':doc['title']})
            return {'buckets':items[offset:offset+limit],'count':len(items),'offset':offset,'limit':limit,'include_archive':include_archive}

    @router.post('/api/serein/memory-projection')
    def projection(body: dict):
        names = identity(settings.database)
        with Reader(settings.database) as reader:
            items=[]
            marks={r['key']:json.loads(r['payload_json']) for r in reader.store.conn.execute("SELECT * FROM personal_records WHERE scope='favorite' AND deleted=0")}
            annotations={}
            for r in reader.store.conn.execute("SELECT * FROM personal_records WHERE scope='annotation' AND deleted=0 ORDER BY created_at,key"):
                annotations.setdefault(r['document_id'],[]).append({**json.loads(r['payload_json']),'id':r['key'],'revision':r['revision']})
            for row in reader.store.conn.execute("SELECT id FROM documents WHERE kind='scene' AND lifecycle!='deleted'"):
                obj=reader.read(row[0],with_evidence=False)
                if not obj['readable']:continue
                doc=obj['document'];meta=scene_payload(doc)['metadata']
                items.append({'source_id':row[0],'title':doc['title'],'date':str(meta.get('date') or meta.get('event_date') or meta.get('created') or '')[:10],
                    'content':doc['body_md'].strip(),'author':str(meta.get('author') or names['ai_name']),'object_kind':'scene',
                    'memory_role':meta.get('memory_role', 'scene'), 'semantic_kind':meta.get('memory_role', 'scene'),
                    'status':'已沉底' if obj['status']=='archived' else '可浮现','storage_status':obj['status'],
                    'type':meta['type'],'active':meta['active'],'scene_status':meta['scene_status'],'status_consistent':True,
                    'bucket_domain':canonical_domain(meta),
                    'self_anchor':bool(meta.get('self_anchor') or meta.get('migration_source_self_anchor')),
                    'favorite':marks.get(row[0],{}).get('favorite',False),'annotations':annotations.get(row[0],[]),
                    'updated_at':doc['updated_at'],'scene_cues':meta.get('scene_cues') or [],'content_hash':digest(doc['body_md'].strip()),
                    'evidence_count':reader.store.conn.execute('SELECT count(*) FROM evidence_bindings WHERE document_id=? AND active=1',(row[0],)).fetchone()[0]})
            # Only edges with current endpoint snapshots may reach the UI.
            edges=[]
            for row in reader.store.conn.execute("SELECT * FROM scene_relations WHERE active=1 AND lifecycle='active'"):
                edge=json.loads(row['metadata_json'])
                owners=[reader.read(row[field],kind='scene',with_evidence=False) for field in ('source_scene_id','target_scene_id')]
                if not all(o['readable'] and o['status']=='active' for o in owners):continue
                from ..compat.germany.scene_linker import SceneEdgeStore
                if not SceneEdgeStore._current_edge_error(edge,*(scene_payload(obj['document']) for obj in owners)):
                    edges.append({**edge,'source':row['source_scene_id'],'target':row['target_scene_id'],'graph_scope':'scene'})
            return {'status':'ok','source':'Serein SQLite live','snapshotId':digest(json.dumps(items,sort_keys=True)), 'scenes':items,'edges':edges}

    @router.post('/v1/tools/call')
    async def call_tool(body: dict):
        name=body.get('name');args=body.get('arguments') or {}
        tools={'write_scene':scenes.write,'edit_scene':scenes.edit,'set_scene_status':scenes.edit,
               'read_scene_evidence':scenes.evidence,'bind_scene_evidence':scenes.bind,'unbind_scene_evidence':scenes.unbind}
        if name in tools:return {'result':tools[name](**args)}
        if name=='read_memory':
            kind,key=args['memory_type'],args['memory_id']
            if kind=='scene':result=scenes.read(key)
            elif kind in ('fact','event'):
                item=Events(settings.database).read(key,include_sources=args.get('include_content',True))
                if item:
                    from .live import event_surface_states
                    event_surface_states(settings.database,{'items':[item]})
                result={'status':'ok' if item else 'not_found','mode':'fact_event','ordinary_recall':False,'item':item}
            elif kind=='narrative':
                from .narratives import endpoints
                from ..compat.narratives import narrative_transaction
                with narrative_transaction(settings.database) as rolls:
                    result=await endpoints(settings,rolls)._read_narrative_memory(key)
            else:result=services.read(key,kind=kind)
            return {'result':result}
        if name=='read_arc_materials':return {'result':services.arc_picks(**args)}
        if name=='breath':
            query=str(args.get('query') or '').strip()
            result=await asyncio.to_thread(services.recall,query,method='semantic',mode='surface',min_cosine=.5,
                limit=max(1,min(10,int(args.get('max_results',2)))))
            text='\n---\n'.join(f"[bucket_id:{card['id']}] {card['title']}\n{card['text']}" for card in result['cards'])
            return {'result':text or '没有找到可靠命中。'}
        if name in ('read_diary','write_diary','revise_diary','comment_diary'):
            from ..compat.diaries import Diaries
            diaries=Diaries(settings.database)
            method={'read_diary':diaries.read,'write_diary':diaries.create,
                'revise_diary':diaries.revise,'comment_diary':diaries.comment}[name]
            return {'result':method(**args)}
        if name in ('narrative_revision_inbox','review_narrative_revision'):
            from ..compat.narratives import narrative_transaction, RevisionInbox
            from ..compat.narrative_review import review_revision
            with narrative_transaction(settings.database,write=name=='review_narrative_revision') as rolls:
                inbox=RevisionInbox(rolls.store)
                return {'result':inbox.list(**args) if name=='narrative_revision_inbox' else review_revision(rolls,**args)}
        raise HTTPException(404,'Tool not provided by this backend')

    @router.post('/api/buckets/bulk-update')
    def domain(body: dict):
        ids=body.get('bucket_ids') or []
        domain=body.get('domain')
        if len(ids)!=1 or not isinstance(domain,str) or not domain.strip():
            raise HTTPException(400,'One Scene and an explicit domain are required')
        current=scenes.read(ids[0])
        if current.get('status')=='not_found':raise HTTPException(404,'Scene not found')
        result=scenes.edit(ids[0],current['metadata']['updated_at'],metadata={'canonical_domain':domain,'domain':[domain]})
        return {'success':result['status'] in ('updated','unchanged'),'updated':int(result['status']=='updated'),'results':[result]}

    @router.post('/api/buckets/delete')
    def delete(body: dict):
        ids=body.get('bucket_ids')
        if body.get('confirm')!='DELETE' or not isinstance(ids,list) or not 1<=len(ids)<=200:
            raise HTTPException(400,'A confirmed, nonempty list of at most 200 Scene IDs is required')
        counts={key:0 for key in ('deleted','skipped','not_found','invalid','failed')}
        results=[]
        for key in dict.fromkeys(str(value or '').strip() for value in ids):
            if not re.fullmatch(r'[A-Za-z0-9_.:#-]{1,160}',key):
                result={'id':key,'status':'invalid'}
            else:
                current=scenes.read(key)
                if current.get('status')=='not_found':
                    result={'id':key,'status':'not_found'}
                else:
                    saved=scenes.edit(key,current['metadata']['updated_at'],status='deleted')
                    result={'id':key,'status':'deleted' if saved['status']=='updated' else 'failed','detail':saved}
            counts[result['status']]+=1
            results.append(result)
        return {**counts,'results':results}

    return router
