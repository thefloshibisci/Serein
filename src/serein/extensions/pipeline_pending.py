"""Unfinished work stays out of memory and reserves its originals until settled."""
from copy import deepcopy
import json
import re
from uuid import uuid4

from ..core.store import Store, Conflict, encode, digest, now

ARCHIVE_CONFIRM='ARCHIVE_PENDING_ORIGINALS'
REOPEN_CONFIRM='RESTORE_PENDING_ORIGINALS'
_UNFINISHED_BATCH_STATES=('pending','routing_only','retry_wait','needs_repair','paused_failure','waiting_materials')


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_pending_materials(
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL UNIQUE REFERENCES pipeline_batches(id) ON UPDATE CASCADE ON DELETE CASCADE,
        scope TEXT NOT NULL, status TEXT NOT NULL, kind TEXT NOT NULL,
        source_ids_json TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL,
        resolved_event_ids_json TEXT NOT NULL DEFAULT '[]')''')
    conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_pending_archive_receipts(
        pending_id INTEGER PRIMARY KEY REFERENCES pipeline_pending_materials(id),
        receipt_json TEXT NOT NULL)''')


def _archive_receipt(conn,pending_id):
    row=conn.execute('SELECT receipt_json FROM pipeline_pending_archive_receipts WHERE pending_id=?',(pending_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _archive_check(conn,pending_id,*,reopening=False):
    """Check exact retained ownership, independently of the old runtime version."""
    from . import pipeline as p
    from .pipeline_recovery import _message_key
    row=conn.execute('''SELECT pm.*,b.input_json,b.result_json,b.status AS batch_status
        FROM pipeline_pending_materials pm JOIN pipeline_batches b ON b.id=pm.batch_id WHERE pm.id=?''',(pending_id,)).fetchone()
    if row is None or row['status']!=('archived_only' if reopening else 'open'):raise Conflict('待办已被领取或结束，请刷新')
    if row['batch_status'] not in ('done',*_UNFINISHED_BATCH_STATES):raise Conflict('原批次状态不允许结束待办')
    data=json.loads(row['input_json']);values=json.loads(row['source_ids_json'])
    if not isinstance(values,list) or not values or any(type(i) is not int or i<1 for i in values) or len(set(values))!=len(values):
        raise Conflict('待办原话范围无效，未结束整理')
    ids=set(values);frozen={m['id']:m for m in data.get('routing_messages',[])}
    owned={m['id'] for m in data.get('messages',data.get('routing_messages',[]))}
    if not ids.issubset(owned) or not ids.issubset(frozen):raise Conflict('待办原话不属于本批冻结所有权')
    originals=[]
    receipt=_archive_receipt(conn,pending_id) if reopening else None
    if reopening and (not isinstance(receipt,dict) or receipt.get('source_ids')!=sorted(ids)
            or receipt.get('batch_id')!=row['batch_id'] or not isinstance(receipt.get('source_sha256'),dict)):
        raise Conflict('缺少匹配的保留原话回执，未恢复')
    for key in sorted(ids):
        raw=conn.execute('SELECT * FROM raw_events WHERE id=?',(key,)).fetchone()
        if raw is None or _message_key(p.message(raw))!=_message_key(frozen[key]):raise Conflict('待办原话已变化，未结束整理')
        processed=conn.execute('SELECT * FROM raw_processing WHERE raw_id=?',(key,)).fetchone()
        if reopening:
            if receipt.get('source_sha256',{}).get(str(key))!=digest(encode(dict(raw))):
                raise Conflict('原话与保留回执不一致，未恢复')
            if not processed or processed['operation_id']!=receipt.get('operation_id') or processed['outcome']!='archive_only':
                raise Conflict('原话已经另行处理，未恢复')
            # A saved Event can cite a shared bridge while another corridor
            # still defers it. Only its exact archive ownership is undone;
            # existing Event/source bindings stay available to normal review.
        elif processed:raise Conflict('待办原话已有结算记录，未重复处理')
        originals.append(dict(raw))
    for other in conn.execute("SELECT id,source_ids_json FROM pipeline_pending_materials WHERE id<>? AND status IN ('open','running')",(pending_id,)):
        if ids.intersection(json.loads(other['source_ids_json'])):raise Conflict('原话还被其他待办预留，请先处理共享依赖')
    placeholders=','.join('?' for _ in _UNFINISHED_BATCH_STATES)
    for batch in conn.execute(f'SELECT id,input_json FROM pipeline_batches WHERE id<>? AND status IN ({placeholders})',
                              (row['batch_id'],*_UNFINISHED_BATCH_STATES)):
        other=json.loads(batch['input_json'])
        if ids.intersection(m['id'] for m in other.get('messages',other.get('routing_messages',[]))):
            raise Conflict('其他未完成批次拥有这些原话，未结束整理')
        requests=[{'component':c} for c in other.get('components',[])]
        outputs=[]
        for job in conn.execute('SELECT request_json,output_json FROM pipeline_jobs WHERE batch_id=?',(batch['id'],)):
            requests.append(json.loads(job['request_json']))
            if job['output_json']:outputs.append(json.loads(job['output_json']))
        for request in requests:
            component=request.get('component') or {}
            if pending_id in component.get('adopted_pending_ids',[]) or (component.get('pending_material_read') or {}).get('id')==pending_id:
                raise Conflict('其他批次正在读取或承接此待办，未结束整理')
            if ids.intersection((request.get('event') or {}).get('source_message_ids',[])):
                raise Conflict('其他 Writer 已领取这些来源，未结束整理')
        for output in outputs:
            if pending_id in output.get('adopt_pending_material_ids',[]) or any(ids.intersection(e.get('source_message_ids',[])) or
                ids.intersection(b.get('source_message_id') for b in e.get('source_bindings',[])) for e in output.get('events',[])):
                raise Conflict('其他 Curator 已承接这些来源，未结束整理')
    if row['batch_status']!='done' and any(key not in ids and not conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=?',(key,)).fetchone() for key in owned):
        raise Conflict('原批次还有不属于此待办的未处理原话，未结束整批')
    return row,data,ids,originals


def check_archive(database,pending_id,confirm):
    if type(pending_id) is not int or pending_id<1 or confirm!=ARCHIVE_CONFIRM:raise ValueError('请明确确认保留原话并结束此待办整理')
    # Do not run the general initializer here: a repeated acknowledgement must
    # not compact the archived batch or rewrite unrelated old frozen plans.
    with Store(database) as store:initialize(store.conn)
    with Store(database,read_only=True) as store:
        if not _archive_receipt(store.conn,pending_id):_archive_check(store.conn,pending_id)


def _archive_in_store(conn,pending_id):
    """Atomic terminal receipt; no source, Event, route or accepted output is deleted."""
    from datetime import datetime,timezone
    previous=_archive_receipt(conn,pending_id)
    if previous:return previous
    row,data,ids,originals=_archive_check(conn,pending_id)
    proof={'reason':'user_confirmed'}
    stamp=datetime.now(timezone.utc).isoformat()
    operation='archive_pending:'+str(pending_id)+':'+digest(row['batch_id'])[:16]
    receipt={'status':'archived_only','pending_id':pending_id,'batch_id':row['batch_id'],
        'operation_id':operation,'originals':len(ids),'source_ids':sorted(ids),'automatic':False,
        'archived_at':stamp,'proof':proof,'originals_preserved':True,'events_changed':0,
        'source_sha256':{str(raw['id']):digest(encode(raw)) for raw in originals}}
    for key in sorted(ids):conn.execute('INSERT INTO raw_processing VALUES (?,?,?)',(key,operation,'archive_only'))
    conn.execute('INSERT INTO pipeline_pending_archive_receipts VALUES (?,?)',(pending_id,encode(receipt)))
    conn.execute("UPDATE pipeline_pending_materials SET status='archived_only',reason=?,updated_at=? WHERE id=?",
                 ('原话已保留，已结束此待办整理；未删除原话或修改已保存 Event。',stamp,pending_id))
    # Completed batches can already contain saved Events: their result stays exact.
    if row['batch_status']!='done':
        result={'status':'archived_only','batch_id':row['batch_id'],'completed_at':stamp,
                'archive_receipt_id':pending_id,'previous_result':json.loads(row['result_json'] or '{}')}
        conn.execute("UPDATE pipeline_batches SET status='done',result_json=? WHERE id=?",(encode(result),row['batch_id']))
    return receipt


def archive(database,pending_id,confirm):
    check_archive(database,pending_id,confirm)
    with Store(database) as store,store.transaction(immediate=True):
        receipt=_archive_in_store(store.conn,pending_id)
        return {'status':'archived_only','pending_id':pending_id,'batch_id':receipt['batch_id'],
                'originals':receipt['originals'],'closure_receipt':receipt}


def check_reopen(database,pending_id,confirm):
    if type(pending_id) is not int or pending_id<1 or confirm!=REOPEN_CONFIRM:
        raise ValueError('请明确确认恢复这项原话整理')
    with Store(database) as store:initialize(store.conn)
    with Store(database,read_only=True) as store:
        if not store.conn.execute('SELECT 1 FROM background_state WHERE name=?',('pending_reopen:'+str(pending_id),)).fetchone():
            _archive_check(store.conn,pending_id,reopening=True)


def reopen(database,pending_id,confirm):
    """Restore one mistaken finish; keep its old terminal receipt and exact text."""
    check_reopen(database,pending_id,confirm)
    key='pending_reopen:'+str(pending_id)
    with Store(database) as store,store.transaction(immediate=True):
        prior=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',(key,)).fetchone()
        if prior:return json.loads(prior[0])
        row,data,ids,originals=_archive_check(store.conn,pending_id,reopening=True)
        receipt=_archive_receipt(store.conn,pending_id)
        batch_id='pipeline:'+uuid4().hex
        fresh=deepcopy(data)
        fresh['messages']=[m for m in data.get('messages',data['routing_messages']) if m['id'] in ids]
        fresh['pending_previous_batch_id']=row['batch_id']
        fresh.pop('resume_pending_id',None)
        fresh.pop('task_snapshot_compacted',None)
        # A new retained item avoids rewriting the original closure receipt or
        # pretending an old accepted plan has the current runtime contract.
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status,result_json) VALUES (?,?,?,'done',?)",
            (batch_id,row['scope'],encode(fresh),encode({'status':'restored_pending','original_pending_id':pending_id})))
        new_id=store.conn.execute("INSERT INTO pipeline_pending_materials(batch_id,scope,status,kind,source_ids_json,reason,updated_at) "
            "VALUES (?,?,'open',?,?,?,?)",(batch_id,row['scope'],row['kind'],encode(sorted(ids)),
             '已恢复误操作保留的原话，可按当前设置继续整理。',now())).lastrowid
        for raw_id in sorted(ids):
            store.conn.execute("DELETE FROM raw_processing WHERE raw_id=? AND operation_id=? AND outcome='archive_only'",
                               (raw_id,receipt['operation_id']))
        result={'status':'reopened','original_pending_id':pending_id,'pending_id':new_id,'batch_id':batch_id,
                'originals':len(originals),'originals_preserved':True,'replan_required':True,
                'source_ids':sorted(ids),'restored_at':now()}
        store.conn.execute('INSERT INTO background_state(name,value_json) VALUES (?,?)',(key,encode(result)))
        return result


def retain_legacy_batches(conn):
    """Expose old unfinished work even before the first post-upgrade model run."""
    from . import pipeline as p
    revision=p.runtime_revision()
    for row in conn.execute("""SELECT * FROM pipeline_batches
        WHERE status IN ('pending','routing_only','retry_wait','needs_repair')
          AND (status<>'needs_repair' OR COALESCE(json_extract(result_json,'$.repair_kind'),'')<>'runtime_contract')
          AND (json_extract(input_json,'$.contract') IS NULL OR json_extract(input_json,'$.contract')<>?
               OR json_extract(input_json,'$.runtime_revision') IS NULL OR json_extract(input_json,'$.runtime_revision')<>?)""",
        (p.CONTRACT,revision)).fetchall():
        if p.runtime_compatible(json.loads(row['input_json'])):continue
        detail={'status':'needs_repair','batch_id':row['id'],'scope':row['scope'],'repair_kind':'runtime_contract',
                'reason':'版本已更新，旧任务原话与输出已保留；可按当前设置重新整理或预览拆批。',
                'note':'其他聊天可继续，旧任务不会自动重发。'}
        if row['status']=='needs_repair':detail['previous_result']=json.loads(row['result_json'] or '{}')
        conn.execute("UPDATE pipeline_batches SET status='needs_repair',result_json=? WHERE id=?",(encode(detail),row['id']))
    for row in conn.execute("SELECT b.* FROM pipeline_batches b WHERE b.status IN ('needs_repair','paused_failure','waiting_materials') AND NOT EXISTS (SELECT 1 FROM pipeline_pending_materials pm WHERE pm.batch_id=b.id)").fetchall():
        data=json.loads(row['input_json'])
        ids=[m['id'] for m in data.get('messages',data.get('routing_messages',[]))
             if not conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=?',(m['id'],)).fetchone()]
        retain(conn,dict(row),data,ids,json.loads(row['result_json'] or '{}').get('reason','原有未完成批次'),publish_routes=False)


def reservation_clause(alias='r'):
    return " AND NOT EXISTS (SELECT 1 FROM pipeline_pending_materials pm, json_each(pm.source_ids_json) ps WHERE pm.status IN ('open','running') AND ps.value="+alias+".id)"


def retain(conn, batch, data, source_ids, reason, *, publish_routes=True):
    """Retain the existing frozen batch/jobs, without copying model prompts."""
    from . import pipeline as p
    ids=sorted(set(source_ids))
    if not ids:return
    routed=data.get('routing_result')
    if routed:
        try:p.validate_routing_result(data,routed)
        except ValueError:routed=None
        else:
            from .pipeline_recovery import record_routes
            if publish_routes:record_routes(conn,batch['id'],routed['assignments'],preserve_existing=True)
    needed=[index for index,component in enumerate(data.get('components',[]))
            if set(ids).intersection(m['id'] for m in component['messages'])]
    accepted=bool(needed) and all(conn.execute("SELECT 1 FROM pipeline_jobs WHERE batch_id=? AND role IN (?,?) AND output_json IS NOT NULL AND json_extract(output_json,'$._host_curator_omission') IS NULL AND json_extract(output_json,'$.context_request') IS NULL",
        (batch['id'],f'event_curator:{index}',f'event_curator:{index}:context')).fetchone() for index in needed)
    kind='writing' if routed and accepted else 'routing' if routed else 'unrouted'
    conn.execute('''INSERT INTO pipeline_pending_materials(batch_id,scope,status,kind,source_ids_json,reason,updated_at)
        VALUES (?,?,'open',?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET status='open',kind=excluded.kind,
        source_ids_json=excluded.source_ids_json,reason=excluded.reason,updated_at=excluded.updated_at''',
        (batch['id'],batch['scope'],kind,encode(ids),str(reason)[:1000],now()))


def park(database,batch,error):
    from . import pipeline as p
    with Store(database) as store,store.transaction(immediate=True):
        row=store.conn.execute('SELECT * FROM pipeline_batches WHERE id=?',(batch['id'],)).fetchone()
        data=json.loads(row['input_json'])
        ids=[m['id'] for m in data.get('messages',data.get('routing_messages',[]))
             if not store.conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=?',(m['id'],)).fetchone()]
        retain(store.conn,dict(row),data,ids,error)
        previous=json.loads(row['result_json'] or '{}')
        result={key:previous[key] for key in ('job_id','failures') if key in previous}
        result.update({'status':'retained','batch_id':batch['id'],'reason':str(error)[:1000],
                'pending':len(ids),'note':'材料及最后错误输出已保留在待处理区，不再自动重复请求。'}
        )
        store.conn.execute("UPDATE pipeline_batches SET status='waiting_materials',result_json=? WHERE id=?",(encode(result),batch['id']))
        return result


def source_locator(messages):
    """Small identifying excerpts for the UI; full originals remain on demand."""
    if not messages:return {}
    first,last=messages[0],messages[-1]
    longest=max(messages,key=lambda m:len(m.get('content','')))
    return {'source':first.get('source',''),'session_id':first.get('original_session_id',first.get('session_id','')),
            'timestamp_source':(first.get('metadata') or {}).get('timestamp_source',''),
            'first_at':first.get('created_at'),'last_at':last.get('created_at'),
            'first_raw_id':first['id'],'last_raw_id':last['id'],
            'preview':first.get('content','')[:160],
            'longest_message':{'id':longest['id'],'role':longest.get('role',''),
                               'chars':len(longest.get('content','')),'preview':longest.get('content','')[:160]}}


def entries(conn, *, limit=100):
    from . import pipeline as p
    compatible_revisions={p.runtime_revision(),*p.EXECUTION_COMPATIBLE_REVISIONS}
    rows=conn.execute('''SELECT pm.*,b.input_json,b.result_json,b.status AS batch_status FROM pipeline_pending_materials pm
        JOIN pipeline_batches b ON b.id=pm.batch_id ORDER BY CASE WHEN pm.status IN ('open','running') THEN 0 ELSE 1 END,pm.id DESC LIMIT ?''',(limit,)).fetchall()
    result=[]
    for row in rows:
        data=json.loads(row['input_json']);ids=json.loads(row['source_ids_json'])
        cards=data.get('routing_result',{}).get('tracks',data.get('tracks',[]))
        source=[m for m in data.get('routing_messages',[]) if m['id'] in ids]
        attempt=last_attempt(conn,row['batch_id'],data,compact=True)
        result.append({key:row[key] for key in ('id','batch_id','scope','status','kind','reason','updated_at')} |
            {'source_ids':ids,'originals':len(ids),'original_chars':sum(len(m['content']) for m in source),
             'locator':source_locator(source),'batch_status':row['batch_status'],
             'plan_compatible':data.get('contract')==p.CONTRACT and data.get('runtime_revision') in compatible_revisions,
             'can_archive':row['status']=='open' and row['batch_status'] in ('done',*_UNFINISHED_BATCH_STATES),
             'closure_receipt':_archive_receipt(conn,row['id']),
             'can_skip':row['batch_status'] in ('pending','paused_failure','retry_wait','routing_only','needs_repair','waiting_materials'),
             'summary':'；'.join(str(c.get('subject',''))+': '+str(c.get('throughline','')) for c in cards)[:240],
             'last_attempt':dict(attempt) if attempt else None,
             'resolved_event_ids':json.loads(row['resolved_event_ids_json'])})
    return result


def last_attempt(conn,batch_id,data,*,compact=False,errors_only=False):
    columns='a.id,a.attempt,a.created_at,a.error,length(a.output_text) AS output_chars,j.role' if compact else 'a.*,j.role'
    # A resumed completed batch gets a new ID; keep its previous output visible.
    seen=set()
    while batch_id and batch_id not in seen:
        seen.add(batch_id)
        attempt=conn.execute(f'''SELECT {columns} FROM pipeline_attempts a JOIN pipeline_jobs j ON j.id=a.job_id
            WHERE j.batch_id=? AND a.error<>'curator_omission_retry_reset' '''+ ("AND a.error<>'' " if errors_only else '')+
            'ORDER BY a.id DESC LIMIT 1',(batch_id,)).fetchone()
        if attempt:return attempt
        batch_id=data.get('pending_previous_batch_id')
        previous=conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone() if batch_id else None
        data=json.loads(previous[0]) if previous else {}


def recover_runs(conn,work_status):
    if work_status not in ('queued','running'):
        conn.execute("UPDATE pipeline_pending_materials SET status='open' WHERE status='running' AND datetime(updated_at)<datetime('now','-1 minute')")


def detail(database,pending_id):
    with Store(database,read_only=True) as store:
        row=store.conn.execute('SELECT * FROM pipeline_pending_materials WHERE id=?',(pending_id,)).fetchone()
        if row is None:raise ValueError('找不到待处理材料')
        batch=store.conn.execute('SELECT input_json,status FROM pipeline_batches WHERE id=?',(row['batch_id'],)).fetchone()
        data=json.loads(batch['input_json']);ids=set(json.loads(row['source_ids_json']))
        messages=[m for m in data.get('routing_messages',[]) if m['id'] in ids]
        jobs=[{'role':j['role'],'output':json.loads(j['output_json']) if j['output_json'] else None}
              for j in store.conn.execute('SELECT role,output_json FROM pipeline_jobs WHERE batch_id=? ORDER BY rowid',(row['batch_id'],))]
        attempt=last_attempt(store.conn,row['batch_id'],data)
        error=last_attempt(store.conn,row['batch_id'],data,errors_only=True)
        return {'id':row['id'],'batch_id':row['batch_id'],'status':row['status'],'batch_status':batch['status'],
                'kind':row['kind'],'reason':row['reason'],'locator':source_locator(messages),
                'originals':len(messages),'original_chars':sum(len(m['content']) for m in messages),'messages':messages,
                'jobs':jobs,'last_attempt':dict(attempt) if attempt else None,
                'closure_receipt':_archive_receipt(store.conn,pending_id),
                'last_error':dict(error) if error else None}


def catalog(database,component):
    tracks=set(component['track_ids']);first=min(m['id'] for m in component['messages'])
    with Store(database,read_only=True) as store:
        result=[]
        for row in store.conn.execute("SELECT pm.*,b.input_json FROM pipeline_pending_materials pm JOIN pipeline_batches b ON b.id=pm.batch_id WHERE pm.status='open' AND pm.kind IN ('writing','routing') AND b.status IN ('done','waiting_materials') ORDER BY pm.id"):
            ids=json.loads(row['source_ids_json'])
            if not ids or max(ids)>=first:continue
            data=json.loads(row['input_json']);parts=data.get('components',[])
            used=[c for c in parts if set(ids).intersection(m['id'] for m in c['messages'])]
            if not used or not set(t for c in used for t in c['track_ids']).issubset(tracks):continue
            result.append({'pending_material_id':row['id'],'source_range':[min(ids),max(ids)],'originals':len(ids),
                'summary':'；'.join(str(c.get('subject',''))+': '+str(c.get('throughline','')) for c in data['routing_result']['tracks'])[:200],
                'original_chars':sum(len(m['content']) for m in data['routing_messages'] if m['id'] in ids)})
            if len(result)>=8:break
        return result


def read_material(database,component,pending_id):
    if type(pending_id) is not int or pending_id not in {r['pending_material_id'] for r in component.get('pending_materials',[])}:
        raise ValueError('待办编号必须来自本轮待处理材料目录')
    with Store(database,read_only=True) as store:
        row=store.conn.execute("SELECT pm.*,b.input_json FROM pipeline_pending_materials pm JOIN pipeline_batches b ON b.id=pm.batch_id WHERE pm.id=? AND pm.status='open'",(pending_id,)).fetchone()
        if row is None:raise Conflict('待处理材料已变化，请重新领取任务')
        data=json.loads(row['input_json']);ids=set(json.loads(row['source_ids_json']))
        messages=[m for m in data['routing_messages'] if m['id'] in ids]
        from .pipeline_recovery import _message_key
        from . import pipeline as p
        p.validate_routing_result(data,data['routing_result'])
        if p._component_signature(data.get('components',[]))!=p._component_signature(p.components(database,data,data['routing_result'],include_materials=False)):
            raise ValueError('待办归线与切分范围不一致，请先重新校验')
        for m in messages:
            raw=store.conn.execute('SELECT * FROM raw_events WHERE id=?',(m['id'],)).fetchone()
            if raw is None or _message_key(p.message(raw))!=_message_key(m):raise Conflict('待处理原话已变化，请先核对')
            if store.conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=?',(m['id'],)).fetchone():raise Conflict('待处理原话已有结算记录')
        memberships=[u for c in data.get('components',[]) for u in c['memberships']
                     if set(u.get('source_message_ids',[u['unit_root_message_id']])).intersection(ids)]
        existing={u['unit_root_message_id']:u for u in component['memberships']}
        for unit in memberships:
            if not set(unit.get('source_message_ids',[unit['unit_root_message_id']])).issubset(ids):
                raise ValueError('待办跨越已结算的归属单元，不能整份承接')
            existing.setdefault(unit['unit_root_message_id'],unit)
        context={m['id']:m for m in component['context_messages']};context.update({m['id']:m for m in messages})
        return {**component,'context_messages':list(context.values()),'memberships':list(existing.values()),
                'context_session_ids':list({m['session_id'] for m in context.values()}),
                'pending_material_read':{'id':pending_id,'messages':messages},
                'curator_read_source_ids':sorted(context)}


def adopted_component(component,output):
    ids=output.get('adopt_pending_material_ids',[])
    if not isinstance(ids,list) or any(type(i) is not int for i in ids) or len(ids)!=len(set(ids)):
        raise ValueError('adopt_pending_material_ids 必须是不重复的待办编号数组')
    reading=component.get('pending_material_read')
    if ids and (not reading or ids!=[reading['id']]):raise ValueError('只能整份承接本次已读取的待办，不能只凭目录编号承接')
    if not ids:return component
    messages={m['id']:m for m in component['messages']};messages.update({m['id']:m for m in reading['messages']})
    return {**component,'messages':list(messages.values()),'adopted_pending_ids':ids}


def check_adoption(component,plan,*,missing=()):
    reading=component.get('pending_material_read')
    if not reading:return
    ids={m['id'] for m in reading['messages']}
    owned={key for event in plan['events'] for key in event['source_message_ids']}
    if component.get('adopted_pending_ids'):
        if not ids.difference(missing).issubset(owned) or ids.intersection(plan['skip_source_message_ids']+plan['defer_source_message_ids']):
            raise ValueError('整份承接必须将待办全部原话纳入 Event，不允许部分承接或 skip/defer')
    elif ids.intersection(owned):raise ValueError('待办仅作背景；写入这些原话前必须明确 adopt_pending_material_ids')


def finish_adoptions(conn,plans,processed,event_ids):
    for component,_,_ in plans:
        for pending_id in component.get('adopted_pending_ids',[]):
            row=conn.execute("SELECT source_ids_json,status FROM pipeline_pending_materials WHERE id=?",(pending_id,)).fetchone()
            if row is None or row['status']!='open':raise Conflict('承接的待办已变化，未结清待办或保存 Event')
            from .pipeline_recovery import _message_key
            from . import pipeline as p
            for message in component['pending_material_read']['messages']:
                raw=conn.execute('SELECT * FROM raw_events WHERE id=?',(message['id'],)).fetchone()
                if raw is None or _message_key(p.message(raw))!=_message_key(message):raise Conflict('承接的原话已变化，未保存 Event')
            if not all(processed.get(i)=='settled' for i in json.loads(row['source_ids_json'])):continue
            conn.execute("UPDATE pipeline_pending_materials SET status='resolved',resolved_event_ids_json=?,updated_at=? WHERE id=?",
                         (encode(event_ids),now(),pending_id))


def _resumable(conn,pending_id,replan):
    """Refusals that need no write; shared by the HTTP precheck and resume."""
    from . import pipeline as p
    from .pipeline_recovery import _message_key
    row=conn.execute("SELECT pm.*,b.input_json,b.status AS batch_status,b.rowid AS queue_order,b.result_json AS previous_result FROM pipeline_pending_materials pm JOIN pipeline_batches b ON b.id=pm.batch_id WHERE pm.id=? AND pm.status='open'",(pending_id,)).fetchone()
    if row is None:raise Conflict('待办已被领取或完成，请刷新')
    data=json.loads(row['input_json']);ids=set(json.loads(row['source_ids_json']))
    if not p.runtime_compatible(data) and not replan:raise Conflict('版本已变化，请选择重新切分，不复用旧执行计划')
    if not ids or not ids.issubset(m['id'] for m in data.get('routing_messages',[])):
        raise Conflict('待处理原话不在完整冻结快照中')
    for m in data.get('routing_messages',[]):
        if m['id'] not in ids:continue
        raw=conn.execute('SELECT * FROM raw_events WHERE id=?',(m['id'],)).fetchone()
        if raw is None or _message_key(p.message(raw))!=_message_key(m):raise Conflict('待处理原话已变化')
        if conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=?',(m['id'],)).fetchone():raise Conflict('待处理原话已结算，不能重复写作')
    return row,data,ids


def check_resume(database,pending_id,*,replan=False):
    """Refuse an impossible request before it can mark the whole pipeline failed."""
    from . import pipeline as p
    p.initialize(database)
    with Store(database,read_only=True) as store:_resumable(store.conn,pending_id,replan)


def resume(database,pending_id,*,replan=False,expire_round_gate=False):
    from . import pipeline as p
    p.initialize(database)
    replan=replan or expire_round_gate
    if replan:
        with Store(database,read_only=True) as store:
            broken=store.conn.execute("""SELECT pm.batch_id FROM pipeline_pending_materials pm
                JOIN pipeline_batches b ON b.id=pm.batch_id
                WHERE pm.id=? AND pm.status='open' AND pm.kind='unrouted' AND b.status='needs_repair'""",
                (pending_id,)).fetchone()
        if broken:
            # No accepted routing can be preserved. Use the existing explicit
            # rebuild so broken cache/history is retired along with the error.
            from .pipeline_recovery import _rebuild
            return {**_rebuild(database,broken['batch_id'],resume_pending_id=pending_id),'status':'resumed'}
    with Store(database) as store,store.transaction(immediate=True):
        row,data,ids=_resumable(store.conn,pending_id,replan)
        batch_id=row['batch_id']
        data['resume_pending_id']=pending_id
        if row['batch_status']=='done' or replan:
            new_id='pipeline:'+uuid4().hex
            fresh=deepcopy(data);fresh.pop('task_snapshot_compacted',None)
            fresh.setdefault('queue_order',row['queue_order'])
            fresh['pending_previous_batch_id']=batch_id
            fresh['messages']=[m for m in data.get('messages',data['routing_messages']) if m['id'] in ids]
            fresh.setdefault('parked',[])
            if replan:
                fresh.update(contract=p.CONTRACT,runtime_revision=p.runtime_revision(),input_policy=p.read_settings(database)['pipeline'])
            if expire_round_gate:
                # Waiting alone does not prove closure. Curator and Writer
                # still judge every retained source under the evidence rules.
                fresh['input_policy']={**fresh['input_policy'],'round_gate_enabled':False}
                fresh['round_gate_expired']=True
            fresh.pop('components',None)
            if fresh.get('routing_result'):fresh['components']=p.components(database,fresh,fresh['routing_result'])
            store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES (?,?,?,'pending')",(new_id,row['scope'],encode(fresh)))
            # Copy only entire unchanged components. Partial components need
            # Curator again, but always reuse the accepted Router snapshot.
            for new_index,component in enumerate(fresh.get('components',[])):
                for old_index,old in enumerate(data.get('components',[])):
                    if replan or p._component_signature([component])!=p._component_signature([old]):continue
                    fresh['components'][new_index]=deepcopy(old)
                    for job in store.conn.execute('SELECT * FROM pipeline_jobs WHERE batch_id=? ORDER BY rowid',(batch_id,)).fetchall():
                        if not re.match(rf'^(?:event_curator:{old_index}(?::context)?$|event_writer:{old_index}:)',job['role']):continue
                        req=json.loads(job['request_json']);req['batch_id']=new_id
                        role=job['role'].replace(':'+str(old_index),':'+str(new_index),1)
                        output=job['output_json']
                        if output and (json.loads(output).get('evidence_sufficient') is False or '_host_curator_omission' in json.loads(output)):output=None
                        store.conn.execute('INSERT INTO pipeline_jobs VALUES (?,?,?,?,?)',(new_id+':'+role,new_id,role,encode(req),output))
            store.conn.execute('UPDATE pipeline_batches SET input_json=? WHERE id=?',(encode(fresh),new_id))
            if row['batch_status']!='done':
                store.conn.execute("UPDATE pipeline_batches SET status='superseded_repair',result_json=? WHERE id=?",
                    (encode({'status':'superseded_repair','replacement_batch_id':new_id,
                             'previous_result':json.loads(row['previous_result'] or '{}')}),batch_id))
            store.conn.execute('UPDATE pipeline_pending_materials SET batch_id=? WHERE id=?',(new_id,pending_id));batch_id=new_id
        else:
            store.conn.execute("UPDATE pipeline_batches SET status=?,result_json=NULL,input_json=? WHERE id=?",('routing_only' if batch_id.startswith('route:') else 'pending',encode(data),batch_id))
            for job in store.conn.execute('SELECT id,output_json FROM pipeline_jobs WHERE batch_id=?',(batch_id,)).fetchall():
                out=json.loads(job['output_json']) if job['output_json'] else {}
                if out.get('evidence_sufficient') is False or '_host_curator_omission' in out:
                    store.conn.execute('UPDATE pipeline_jobs SET output_json=NULL WHERE id=?',(job['id'],))
        store.conn.execute('DELETE FROM pipeline_job_failures WHERE job_id IN (SELECT id FROM pipeline_jobs WHERE batch_id=? AND output_json IS NULL)',(batch_id,))
        store.conn.execute("UPDATE pipeline_pending_materials SET status='running',updated_at=? WHERE id=?",(now(),pending_id))
        return {'status':'resumed','batch_id':batch_id}


def target_dependencies(conn,batch_id,bases,adopting=()):
    dependencies=[]
    if not bases:return dependencies
    for row in conn.execute("SELECT id,batch_id FROM pipeline_pending_materials WHERE status IN ('open','running') AND batch_id<>?",(batch_id,)):
        if row['id'] in adopting:continue
        for job in conn.execute("SELECT output_json,request_json FROM pipeline_jobs WHERE batch_id=? AND (role LIKE 'event_curator:%' OR role LIKE 'event_writer:%')",(row['batch_id'],)):
            output=json.loads(job['output_json'] or '{}');request=json.loads(job['request_json'])
            reserved={b for e in output.get('events',[]) for b in e.get('base_event_ids',[])}
            reserved.update(request.get('event',{}).get('base_event_ids',[]))
            if set(bases) & reserved:
                dependencies.append(row['id']);break
    return dependencies


def dependency_context(database,batch_id,component,output):
    """Read one eligible predecessor before Writer; adoption stays a model decision."""
    if output.get('context_request') is not None or component.get('pending_material_read'):return None
    bases={b for e in output.get('events',[]) for b in e.get('base_event_ids',[])}
    eligible={r['pending_material_id'] for r in component.get('pending_materials',[])}
    with Store(database,read_only=True) as store:
        blockers=target_dependencies(store.conn,batch_id,bases)
    return next(({'pending_material_id':i,'reason':'continue_pending'} for i in blockers if i in eligible),None)


def read_dependency_targets(database,batch_id,component):
    """Explain reservations of the material actually read, without granting ownership."""
    reading=component.get('pending_material_read')
    if not reading:return []
    candidates={b.get('event_id') or b.get('item_id') or b.get('id')
                for b in component.get('base_event_candidates',[])}-{None}
    with Store(database,read_only=True) as store:
        return sorted(base for base in candidates
                      if reading['id'] in target_dependencies(store.conn,batch_id,{base}))


def defer_blocked_targets(database,batch_id,component,plan):
    """Preserve blocked ownership while allowing independent Events to settle."""
    result=deepcopy(plan);events=[];deferred=set(result['defer_source_message_ids'])
    owned={m['id'] for m in component['messages']}
    with Store(database,read_only=True) as store:
        for event in result['events']:
            blockers=target_dependencies(store.conn,batch_id,event['base_event_ids'],component.get('adopted_pending_ids',[]))
            if not blockers:events.append(event);continue
            ids=owned.intersection(event['source_message_ids']);deferred.update(ids)
            result.setdefault('host_deferrals',[]).append({'reason':'pending_target_dependency',
                'pending_material_ids':blockers,'source_message_ids':sorted(ids)})
    # A bridge may be shared by multiple Events in this corridor. Never save
    # an Event containing a source that another blocked Event still defers.
    while True:
        overlapping=[e for e in events if deferred.intersection(e['source_message_ids'])]
        if not overlapping:break
        for event in overlapping:
            deferred.update(owned.intersection(event['source_message_ids']));events.remove(event)
    result['events']=events;result['defer_source_message_ids']=sorted(deferred)
    return result


def assert_targets_available(conn,batch_id,plans):
    """Final race check; an unrelated job cannot overtake a retained update."""
    from ..compat.germany.fact_events import FactEventSettlementBlockedError
    adopting={i for component,_,_ in plans for i in component.get('adopted_pending_ids',[])}
    bases={b for _,plan,_ in plans for event in plan['events'] for b in event['base_event_ids']}
    blockers=target_dependencies(conn,batch_id,bases,adopting)
    if blockers:raise FactEventSettlementBlockedError('旧 Event 仍有待补写材料，请先承接或完成待办 '+str(blockers[0]))


def router_context(database,cards,recent,before_id):
    """Keep frozen proofs intact; present retained originals by short reference."""
    reserved=set();tracks={};directory=[]
    with Store(database,read_only=True) as store:
        for row in store.conn.execute("SELECT pm.id,pm.source_ids_json,b.input_json FROM pipeline_pending_materials pm JOIN pipeline_batches b ON b.id=pm.batch_id WHERE pm.status IN ('open','running') ORDER BY pm.id"):
            ids=json.loads(row['source_ids_json'])
            if not ids or max(ids)>=before_id:continue
            reserved.update(ids);data=json.loads(row['input_json'])
            track_ids={t['track_id'] for t in data.get('routing_result',{}).get('tracks',[])}
            for key in track_ids:tracks.setdefault(key,[]).append(row['id'])
            if track_ids.intersection(c['track_id'] for c in cards) and len(directory)<8:
                directory.append({'pending_material_id':row['id'],'source_range':[min(ids),max(ids)],'originals':len(ids)})
    projected=[]
    for card in cards:
        if card['track_id'] in tracks:
            projected.append({**card,'recent_turns':[]})
        else:projected.append(card)
    return projected,[m for m in recent if m['id'] not in reserved],directory
