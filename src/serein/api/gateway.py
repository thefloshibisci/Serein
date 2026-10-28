"""Existing Bridge Hook transport; the host remains delivery authority."""

import time
import math
from fastapi import APIRouter, HTTPException

from ..recall.germany.gateway import GatewayService


def routes(services,auth):
    router=APIRouter(dependencies=auth)

    @router.get('/api/gateway-injections')
    def history(session_id:str='',limit:int=20,before_id:int=0,review_ids:str='',include_context:bool=False,
                after_id:int | None=None):
        from ..compat.gateway_history import GatewayHistory
        if after_id is not None and (after_id < 0 or before_id):
            raise HTTPException(400, 'Use either before_id or nonnegative after_id')
        store=GatewayHistory(services._settings.database)
        limit=max(1,min(100,limit))
        rows=store.list_injection_debug(session_id=session_id,limit=limit+1,before_id=before_id,
            after_id=after_id,include_context=include_context,visible_only=True)
        items=rows[:limit]
        ids=list(dict.fromkeys(int(value) for value in review_ids.split(',') if value.isdigit() and int(value)>0))[:500]
        reviewed=store.list_injection_debug(session_id=session_id,ids=ids,limit=len(ids),include_context=include_context,visible_only=True) if ids else []
        next_id=items[-1]['id'] if items else None
        return {'status':'ok','items':items,'reviewed_items':[row for row in reviewed if row['id'] not in {item['id'] for item in items}],
            'has_more':len(rows)>limit,'next_before_id':next_id,'next_cursor':str(next_id) if next_id else None,
            **({'next_after_id':next_id if next_id is not None else after_id} if after_id is not None else {})}

    @router.post('/api/hook/recall')
    def recall(body: dict):
        query=str(body.get('query') or body.get('message') or body.get('prompt') or '').strip()
        if not query:raise HTTPException(400,'query is required')
        simulation=str(body.get('simulation') or '').lower() in ('true','1','yes')
        threshold=body.get('direct_threshold')
        if 'direct_threshold' in body:
            if not simulation:
                raise HTTPException(400, 'threshold_override_requires_simulation')
            if type(threshold) not in (int,float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise HTTPException(400, 'direct_threshold must be a number between 0 and 1')
        ablation=str(body.get('recall_ablation') or 'normal')
        if ablation not in ('normal','without_cues','without_embedding'):
            raise HTTPException(400,'Unsupported recall ablation')
        if ablation!='normal' and not (simulation and body.get('simulation_scope','full_shadow')=='full_shadow'
            and body.get('recall_mode') in ('full','gateway','parity')):
            raise HTTPException(400,'recall_ablation_requires_simulation_debug_full')
        maximum=max(0,min(5,int(body.get('max_notes',body.get('max_cards',2)))))
        char_limit=max(160,min(2400,int(body.get('max_chars',1200))))
        deadline=None if simulation else time.monotonic()+9.0
        result=services.recall(query,method='semantic',mode='surface',min_cosine=.5,limit=max(1,maximum),
            user_utterance=True,
            delivered_ids=body.get('delivered_ids',[]),exclude_ids=body.get('exclude_ids',[]),
            use_passages=body.get('use_passages'),body_char_limit=char_limit,
            delivered_menu_keys=body.get('delivered_menu_keys',[]),recall_ablation=ablation,
            deadline_at=deadline, **({'threshold_override':threshold} if threshold is not None else {})) if maximum else {'cards':[],'context':'','selected_refs':[]}
        if not maximum:
            from ..core.preferences import Preferences, surface
            result = surface(result, Preferences(services._settings.database).match(query))
        cards=result['cards']
        context=GatewayService._clip_text(result['context'],max(160,min(12000,int(body.get('max_context_chars',4200)))))
        route=result.get('routing',{})
        semantic={'called':True,'route':route.get('route',''),'route_action':route.get('action','recall'),
            'recall_ablation':{'mode':ablation,'authored_cues_enabled':ablation!='without_cues',
                'body_embedding_enabled':ablation!='without_embedding','route_embedding_enabled':True},
            'score':route.get('score'),'margin':route.get('margin'),
            'semantic_recall_router':{'route':route.get('route',''),'action':route.get('action','recall'),
            'score':route.get('score'),'margin':route.get('margin'),'reason':route.get('reason',''),
            'routes':route.get('scores',[])},'pre_candidate_gate':result.get('pre_candidate_gate'),
            'surface_reranker_gate':result.get('surface_reranker_gate')}
        typed={key:value for key,value in result.items() if key not in ('context','full_additional_context','additional_context','pools','cards')}
        return {'ok':True,'query':query,'session_id':str(body.get('session_id') or 'hook'),
            'cards':cards,'notes':cards,'recalled_ids':result['selected_refs'],
            'preferences':result.get('preferences',[]),
            'additional_context':GatewayService._render_hook_recall_full_additional_context(context),
            'debug':{'mode':'full_gateway','semantic_recall_debug':semantic,'typed_event_scene_live':typed,
                     'recalled_bucket_ids':result['selected_refs'],'records_injections':False},'injected':False}

    return router
