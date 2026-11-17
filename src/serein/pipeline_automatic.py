"""Bounded automatic recovery and durable timer observations.

A completed pass is never reported as an empty backlog while retained material
still exists. Gate-only waits can be proved from accepted Curator results and
reviewed without inventing evidence or discarding originals.
"""
import json
import logging
import re
from datetime import datetime, timezone
from .core.store import Store, encode, now


def observe(database, **fields):
    try:
        with Store(database) as store,store.transaction(immediate=True):
            row=store.conn.execute("SELECT value_json FROM background_state WHERE name='pipeline_auto_observation'").fetchone()
            value=json.loads(row[0]) if row else {}
            value.update(fields)
            store.conn.execute("INSERT INTO background_state VALUES ('pipeline_auto_observation',?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",(encode(value),))
    except Exception:
        logging.getLogger(__name__).exception('Automatic Event observation could not be saved')


def resume_gate_wait(database):
    reconcile_covered_pending(database)
    from .extensions.pipeline_agent import _round_gate_only
    from .extensions import pipeline_pending as pending
    with Store(database,read_only=True) as store:
        rows=store.conn.execute("SELECT pm.*,b.input_json,b.result_json,b.status AS batch_status FROM pipeline_pending_materials pm JOIN pipeline_batches b ON b.id=pm.batch_id WHERE pm.status='open' ORDER BY pm.id").fetchall()
        selected=next((row['id'] for row in rows if _round_gate_only(store.conn,row,json.loads(row['input_json']))),None)
    if selected is None:return None
    result=pending.resume(database,selected,expire_round_gate=True)
    observe(database,resumed_pending_id=selected,recovery_at=now())
    return result


def pending_status(database):
    with Store(database,read_only=True) as store:
        rows=store.conn.execute("SELECT id,reason FROM pipeline_pending_materials WHERE status IN ('open','running') ORDER BY id").fetchall()
    if not rows:return None
    return {'status':'pending_materials','pending_count':len(rows),
            'pending_material_ids':[r['id'] for r in rows],
            'reason':'仍有未完成材料，未标记自动整理完成；详情保留具体原因。'}


def resume_transient_failure(database):
    """At most three delayed recovery cycles per exact retained item.

    Transport outages retry; invalid evidence or a deliberate user pause does
    not. The original failed jobs remain available for inspection.
    """
    from .extensions import pipeline_pending as pending
    current=datetime.now(timezone.utc)
    with Store(database,read_only=True) as store:
        rows=store.conn.execute("SELECT pm.id,pm.batch_id,b.input_json FROM pipeline_pending_materials pm JOIN pipeline_batches b ON b.id=pm.batch_id WHERE pm.status='open' AND b.status IN ('done','waiting_materials') ORDER BY pm.id").fetchall()
        for row in rows:
            data=json.loads(row['input_json'])
            error=pending.last_attempt(store.conn,row['batch_id'],data,errors_only=True)
            reason=str(error['error'] if error else '')
            status=re.search(r'\bHTTP\s+(\d{3})\b',reason)
            code=int(status.group(1)) if status else None
            # Providers also use nonstandard 5xx codes (513, 529, ...).
            # Keep the durable retry cap; unsupported operations/protocols
            # and credential/request errors cannot be repaired by waiting.
            retryable_http=code==429 or (code is not None and 500<=code<600 and code not in (501,505))
            if not retryable_http and not any(token in reason for token in ('无法连接上游','超时','timed out','Timeout')):continue
            key='pipeline_auto_retry:'+str(row['id'])
            old=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',(key,)).fetchone()
            receipt=json.loads(old[0]) if old else {'attempts':0}
            if receipt['attempts']>=3:continue
            previous=datetime.fromisoformat(receipt.get('attempted_at') or error['created_at'])
            if (current-previous).total_seconds()<min(3600,300*2**receipt['attempts']):continue
            chosen=(row['id'],key,receipt['attempts']+1);break
        else:return None
    result=pending.resume(database,chosen[0],replan=True)
    with Store(database) as store:
        store.conn.execute('INSERT INTO background_state VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
                           (chosen[1],encode({'attempts':chosen[2],'attempted_at':now()})))
    observe(database,resumed_pending_id=chosen[0],recovery_at=now())
    return result


def reconcile_covered_pending(database):
    """Close ledger-only leftovers only when every exact original is saved.

    No Event body or binding changes. An equal title or paraphrase is never a
    match: source identity, content, role and timestamp must all agree.
    """
    closed=[]
    with Store(database) as store,store.transaction(immediate=True):
        rows=store.conn.execute("SELECT pm.id,pm.batch_id,pm.source_ids_json FROM pipeline_pending_materials pm JOIN pipeline_batches b ON b.id=pm.batch_id WHERE pm.status='open' AND b.status='done'").fetchall()
        for row in rows:
            ids=json.loads(row['source_ids_json']);events=set()
            if not ids:continue
            for key in ids:
                raw=store.conn.execute('SELECT * FROM raw_events WHERE id=?',(key,)).fetchone()
                if raw is None:break
                refs=store.conn.execute("SELECT s.item_id FROM fact_event_sources s JOIN fact_events e ON e.item_id=s.item_id WHERE e.status='active' AND s.source_system=? AND s.session_id=? AND s.message_id=? AND s.content=? AND s.role=? AND s.created_at=?",
                    (raw['source'],raw['session_id'],raw['source_event_id'],raw['text'],raw['role'],raw['created_at'])).fetchall()
                if not refs:break
                events.update(ref['item_id'] for ref in refs)
            else:
                for key in ids:
                    store.conn.execute('INSERT OR IGNORE INTO raw_processing VALUES (?,?,?)',(key,'covered_pending:'+str(row['id']),'settled_existing'))
                store.conn.execute("UPDATE pipeline_pending_materials SET status='resolved',resolved_event_ids_json=?,reason=?,updated_at=? WHERE id=?",
                    (encode(sorted(events)),'原文已逐字绑定到现有事件，仅补齐处理记录，未重复生成。',now(),row['id']))
                closed.append(row['id'])
    return closed
