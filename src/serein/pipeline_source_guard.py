"""Deleted originals revoke frozen work, including reading-only context.

Only identifiers are inspected. Never reconstruct a deleted original from a
snapshot, and never accept an in-flight result after its inputs are revoked.
"""
import json
from .core.store import Store, Conflict, encode, now


def message_ids(value):
    result=set()
    def walk(node):
        if isinstance(node,dict):
            key=node.get('id')
            if type(key) is int and key>0 and node.get('role') in ('user','assistant'):
                result.add(key)
            for key in node.get('recent_source_message_ids',[]):
                if type(key) is int and key>0:result.add(key)
            for child in node.values():walk(child)
        elif isinstance(node,list):
            for child in node:walk(child)
    walk(value)
    return result


def deleted_ids(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_message_tombstones'").fetchone():return set()
    return {row[0] for row in conn.execute('SELECT id FROM raw_message_tombstones')}


def assert_readable(conn, value):
    ids=message_ids(value)
    if ids & deleted_ids(conn):
        raise Conflict('任务引用的原文已被删除；旧快照和旧结果已失效，不得继续使用。')
    for offset in range(0,len(ids),400):
        chunk=sorted(ids)[offset:offset+400]
        rows=conn.execute('SELECT id,metadata_json FROM raw_events WHERE id IN ('+','.join('?' for _ in chunk)+')',chunk).fetchall()
        found={row['id'] for row in rows}
        if set(chunk)-found:raise Conflict('任务来源已不存在；请从仍可读取的原文重新整理。')
        for row in rows:
            meta=json.loads(row['metadata_json'] or '{}')
            if any(meta.get(k) for k in ('draft','discarded','memory_event_source','memory_review_only')):
                raise Conflict('任务来源已不可用于整理，旧结果未保存。')


def check(database, value):
    with Store(database,read_only=True) as store:assert_readable(store.conn,value)


def invalidate_deleted(conn):
    """Retire exact affected plans; surviving originals re-enter normal routing.

    Saved Events are not rewritten. Old task evidence remains private history,
    but cannot be read through pending tools or used in a new model request.
    """
    deleted=deleted_ids(conn)
    if not deleted or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_batches'").fetchone():return []
    marker=encode(sorted(deleted))
    previous=conn.execute("SELECT value_json FROM background_state WHERE name='pipeline_deleted_source_scan'").fetchone()
    if previous and previous[0]==marker:return []
    affected=[];revoked=set()
    prior=conn.execute("SELECT value_json FROM background_state WHERE name='pipeline_deleted_source_tracks'").fetchone()
    if prior:revoked.update(json.loads(prior[0]))
    for row in conn.execute("SELECT id,status,input_json FROM pipeline_batches WHERE status NOT LIKE 'superseded_%'").fetchall():
        data=json.loads(row['input_json'])
        touched=message_ids(data)&deleted
        if not touched:
            for job in conn.execute('SELECT request_json FROM pipeline_jobs WHERE batch_id=?',(row['id'],)):
                if message_ids(json.loads(job[0])) & deleted:
                    touched=True;break
        if not touched:continue
        affected.append(row['id'])
        revoked.update(c['track_id'] for c in data.get('routing_result',{}).get('tracks',[]) if c.get('track_id'))
        conn.execute("UPDATE pipeline_batches SET status='superseded_source_deleted' WHERE id=?",(row['id'],))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_pending_materials'").fetchone():
            conn.execute("UPDATE pipeline_pending_materials SET status='cancelled_source_deleted',reason=?,updated_at=? WHERE batch_id=? AND status IN ('open','running')",
                         ('原文已删除，旧整理计划已取消；其余原文按正常流程重新整理。',now(),row['id']))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_route_provenance'").fetchone():
            conn.execute('DELETE FROM pipeline_routes WHERE raw_id IN (SELECT raw_id FROM pipeline_route_provenance WHERE batch_id=?)',(row['id'],))
            conn.execute('DELETE FROM pipeline_route_provenance WHERE batch_id=?',(row['id'],))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_routes'").fetchone():
        conn.execute('DELETE FROM pipeline_routes WHERE raw_id IN (SELECT id FROM raw_message_tombstones)')
    conn.execute("INSERT INTO background_state VALUES ('pipeline_deleted_source_tracks',?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",(encode(sorted(revoked)),))
    conn.execute("INSERT INTO background_state VALUES ('pipeline_deleted_source_scan',?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",(marker,))
    return affected
