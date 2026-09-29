"""Explicit original-message upload and deletion; never reconstruct missing dialogue."""
import json
import re
from datetime import datetime
from .core.store import Store, Conflict, digest, encode, now
from .compat.raw_archive import raw_archive
from .compat.originals import Originals, raw_id, READABLE


def initialize(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS raw_message_tombstones ('
        'id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_event_id TEXT NOT NULL, '
        'event_hash TEXT NOT NULL, deleted_at TEXT NOT NULL, content_sha256 TEXT NOT NULL)')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS raw_tombstone_identity '
        "ON raw_message_tombstones(source,source_event_id) WHERE source_event_id!=''")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS raw_message_no_resurrection BEFORE INSERT ON raw_events
        WHEN EXISTS (SELECT 1 FROM raw_message_tombstones t WHERE t.source=NEW.source
          AND ((t.source_event_id!='' AND t.source_event_id=NEW.source_event_id) OR t.event_hash=NEW.event_hash))
        BEGIN SELECT RAISE(ABORT,'original message was explicitly deleted'); END""")


class SourceManagement:
    def __init__(self, settings):
        self.settings = settings
        self.archive = raw_archive(settings)
        with Store(settings.database) as store:
            initialize(store.conn)

    def upload(self, client: str, conversation_id: str, messages: list[dict]) -> dict:
        if not self.settings.writable:
            raise ValueError('This deployment is read-only')
        if not isinstance(client,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,60}',client):
            raise ValueError('client must be a stable 1..60 character label')
        if not isinstance(conversation_id,str) or not conversation_id.strip() or len(conversation_id)>200:
            raise ValueError('A stable conversation_id is required')
        if not isinstance(messages,list) or not 1<=len(messages)<=50:
            raise ValueError('Upload 1..50 actual user/assistant messages')
        if sum(len(str(m.get('content',''))) for m in messages if isinstance(m,dict))>200000:
            raise ValueError('Split uploads larger than 200000 characters')
        stamp=now(); prepared=[]; seen=set()
        for message in messages:
            if not isinstance(message,dict) or set(message)-{'message_id','role','content','created_at'}:
                raise ValueError('Use message_id, role, content and optional created_at only')
            mid=message.get('message_id'); role=message.get('role'); content=message.get('content')
            created=message.get('created_at')
            if not isinstance(mid,str) or not mid.strip() or len(mid)>200 or mid in seen:
                raise ValueError('Each message needs a unique stable message_id (reuse it on retry)')
            seen.add(mid)
            if role not in ('user','assistant') or not isinstance(content,str) or not content.strip():
                raise ValueError('Only nonempty verbatim user/assistant text is accepted')
            if created is not None:
                if not isinstance(created,str): raise ValueError('created_at must be ISO8601 or omitted')
                try: parsed=datetime.fromisoformat(created.replace('Z','+00:00'))
                except ValueError: raise ValueError('created_at must be ISO8601 or omitted') from None
                if parsed.tzinfo is None: raise ValueError('created_at needs an explicit timezone')
            identity=digest(encode([client,conversation_id,mid]))
            event={'source':'tool:'+client,'source_event_id':identity,'role':role,'text':content,
                'created_at':created or stamp,'session_id':conversation_id,'conversation_id':conversation_id,
                'client':client,'metadata':{'upload_method':'explicit_tool','upstream_message_id':mid,
                    'original_time_unknown':created is None,'supplied_created_at':created}}
            normalized,reason=self.archive._normalize_event(event,default_source=event['source'],ingested_at=stamp)
            if reason or normalized['text']!=content:
                raise ValueError('Message contains injected context or requires cleanup; submit only the original dialogue text')
            prepared.append(normalized)
        results=[]
        with Store(self.settings.database) as store,store.transaction(immediate=True):
            for event in prepared:
                prior=store.conn.execute('SELECT * FROM raw_events WHERE source=? AND source_event_id=?',
                    (event['source'],event['source_event_id'])).fetchone()
                if store.conn.execute('SELECT 1 FROM raw_message_tombstones WHERE source=? AND source_event_id=?',
                    (event['source'],event['source_event_id'])).fetchone():
                    raise Conflict('An explicitly deleted message cannot be resurrected by retry')
                if prior:
                    old_meta=json.loads(prior['metadata_json']); new_meta=json.loads(event['metadata_json'])
                    if prior['text']!=event['text'] or prior['role']!=event['role'] or old_meta.get('supplied_created_at')!=new_meta.get('supplied_created_at'):
                        raise Conflict('Same message identity has different content, role or time; original preserved')
                    results.append({'id':'raw:'+str(prior['id']),'status':'duplicate'}); continue
                keys=('source','source_event_id','event_hash','role','text','created_at','ingested_at','conversation_id','session_id','client','metadata_json')
                cursor=store.conn.execute('INSERT INTO raw_events ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',tuple(event[k] for k in keys))
                rid=cursor.lastrowid
                if self.archive.fts_enabled:
                    store.conn.execute('INSERT INTO raw_events_fts(rowid,text,source,conversation_id,session_id) VALUES (?,?,?,?,?)',
                        (rid,event['text'],event['source'],event['conversation_id'],event['session_id']))
                results.append({'id':'raw:'+str(rid),'status':'inserted'})
        return {'items':results,'inserted':sum(r['status']=='inserted' for r in results),
            'duplicate':sum(r['status']=='duplicate' for r in results),
            'instruction':'Read returned raw IDs to verify. Upload stores originals; it does not claim automatic complete capture or create a Scene.'}

    @staticmethod
    def blockers(conn,row):
        reasons=[]; sha=digest(row['text'])
        # Conservative protection includes detached and historical evidence copies.
        if conn.execute('SELECT 1 FROM sources WHERE content_sha256=? LIMIT 1',(sha,)).fetchone():
            reasons.append('已有记忆证据副本引用此原话；需先处理引用，不能只删档案造成误解')
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'fact_event_sources' in tables and conn.execute('SELECT 1 FROM fact_event_sources WHERE content_sha256=? LIMIT 1',(sha,)).fetchone():
            reasons.append('已有事件来源引用此原话')
        if 'pipeline_batches' in tables and conn.execute("SELECT 1 FROM pipeline_batches WHERE status IN ('pending','paused_failure') LIMIT 1").fetchone():
            reasons.append('有未完成的原话整理批次；请先完成或处置该批次')
        return reasons

    def preview_delete(self, ids: list[str]) -> dict:
        if not isinstance(ids,list) or not 1<=len(ids)<=20: raise ValueError('Select 1..20 exact raw IDs')
        numbers=list(dict.fromkeys(raw_id(v) for v in ids)); items=[]
        with Store(self.settings.database,read_only=True) as store:
            for number in numbers:
                row=store.conn.execute(f'SELECT * FROM raw_events WHERE id=? AND {READABLE}',(number,)).fetchone()
                if row is None: raise ValueError('Original is missing or not readable')
                items.append({'id':'raw:'+str(number),'sha256':digest(encode(dict(row))),
                    'blockers':self.blockers(store.conn,row)})
        return {'items':items,'can_delete':not any(r['blockers'] for r in items),
            'scope':'删除当前档案中的原话及搜索索引；不会改写记忆、导入文件或既有备份。已有证据引用时拒绝删除。'}

    def delete(self, items: list[dict], confirm: bool) -> dict:
        if not self.settings.writable: raise ValueError('This deployment is read-only')
        if confirm is not True or not isinstance(items,list) or not 1<=len(items)<=20:
            raise ValueError('Preview then explicitly confirm 1..20 original deletions')
        deleted=[]
        with Store(self.settings.database) as store,store.transaction(immediate=True):
            for item in items:
                number=raw_id(item.get('id',''))
                row=store.conn.execute(f'SELECT * FROM raw_events WHERE id=? AND {READABLE}',(number,)).fetchone()
                if row is None or digest(encode(dict(row)))!=item.get('sha256'):
                    raise Conflict('Original changed or disappeared; preview again')
                reasons=self.blockers(store.conn,row)
                if reasons: raise Conflict('; '.join(reasons))
                store.conn.execute('INSERT INTO raw_message_tombstones VALUES (?,?,?,?,?,?)',
                    (number,row['source'],row['source_event_id'],row['event_hash'],now(),digest(row['text'])))
                if self.archive.fts_enabled:
                    store.conn.execute("INSERT INTO raw_events_fts(raw_events_fts,rowid,text,source,conversation_id,session_id) VALUES ('delete',?,?,?,?,?)",
                        (number,row['text'],row['source'],row['conversation_id'],row['session_id']))
                store.conn.execute('DELETE FROM raw_events WHERE id=?',(number,))
                deleted.append('raw:'+str(number))
        return {'deleted_ids':deleted,'count':len(deleted)}
