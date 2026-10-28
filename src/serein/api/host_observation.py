"""Bounded, host-reported diagnostics; never infer delivery from prepared candidates."""
from datetime import datetime
import math
from fastapi import HTTPException


def observation(body):
    raw = body.get('observation')
    if raw is None:
        return None
    def require(ok):
        if not ok:
            raise HTTPException(400, 'Invalid host observation')
    require(isinstance(raw, dict) and type(raw.get('version')) is int and raw['version'] == 1)
    require(all(isinstance(body.get(k), str) and 0 < len(body[k]) <= 250 for k in ('receipt_id','window_id')))
    def text(key, limit):
        value = raw.get(key, '')
        require(isinstance(value, str) and len(value) <= limit)
        return value
    status = text('request_status', 32)
    require(status in ('completed', 'failed', 'interrupted'))
    recall = text('recall_status', 64)
    ids = body['delivered_ids']
    require(len(ids) <= 2 and len(set(ids)) == len(ids))
    candidates = raw.get('candidates', [])
    require(isinstance(candidates, list) and len(candidates) <= 2)
    prepared = []
    for item in candidates:
        require(isinstance(item, dict))
        candidate = {}
        for key, limit in [('id',250), ('title',240), ('source_kind',16)]:
            value = item.get(key, '')
            require(isinstance(value,str) and len(value) <= limit)
            candidate[key] = value
        require(candidate['id'].startswith(('scene:', 'event:')))
        score = item.get('score')
        if score is not None:
            require(type(score) in (int,float) and math.isfinite(score))
            candidate['score'] = score
        prepared.append(candidate)
    require(len({p['id'] for p in prepared}) == len(prepared))
    require(not ids or (status == 'completed' and recall == 'ready' and set(ids) <= {p['id'] for p in prepared}))
    timestamp = text('created_at', 40)
    try:
        require(datetime.fromisoformat(timestamp.replace('Z','+00:00')).tzinfo is not None)
    except ValueError:
        raise HTTPException(400, 'Invalid observation timestamp')
    elapsed = raw.get('elapsed_ms', 0)
    require(type(elapsed) is int and 0 <= elapsed <= 86400000)
    return {'version':1, 'client':'orangechat', 'request_status':status, 'recall_status':recall,
            'created_at':timestamp, 'elapsed_ms':elapsed, 'query':text('query',2000),
            'route':text('route',120), 'reason':text('reason',240), 'candidates':prepared}


def project_observation(payload):
    meta = payload.get('observation')
    if not meta:
        return {}
    ids = payload['delivered_ids']
    outcome = ('failed' if meta['request_status'] != 'completed'
               or meta['recall_status'] not in ('ready','empty') else
               'injected' if ids else 'no_match' if meta['recall_status'] == 'empty' else 'skip')
    return {'observation_version':1, 'reported_by':'orangechat',
            'created_at':meta['created_at'], 'query':meta['query'],
            'request_status':meta['request_status'], 'recall_status':meta['recall_status'],
            'elapsed_ms':meta['elapsed_ms'], 'gateway_memory_route':meta['route'],
            'gateway_memory_trigger':meta['reason'] or meta['recall_status'],
            'gateway_memory_items':[p for p in meta['candidates'] if p['id'] in ids],
            'prepared_items':meta['candidates'], 'hook_memory_outcome':outcome}
