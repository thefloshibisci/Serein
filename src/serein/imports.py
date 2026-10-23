"""Explicit file imports, with durable progress and original-content preservation.

Format/UUID contracts were checked against import_memory.py at ad60c5a.
This adapter uses the public raw archive and Scene store, never the old dehydrator.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .core.store import Store, Conflict, digest, encode, now
from .deployment import identity
from .compat.germany.raw_events import RawEventStore

DEFAULT_BATCH_SIZE=25
LARGE_BATCH_SIZE=250
MAX_BATCH_CHARS=1_000_000


def timestamp(value, fallback):
    if value in (None,''):return fallback
    if isinstance(value,(int,float)) or (isinstance(value,str) and re.fullmatch(r'\d+(?:\.\d+)?',value)):
        numeric=float(value)
        return datetime.fromtimestamp(numeric/1000 if numeric>100_000_000_000 else numeric,timezone.utc).isoformat()
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()


def text_content(value):
    if isinstance(value,str):return value
    if isinstance(value,dict):
        if isinstance(value.get('text'),str):return value['text']
        return text_content(value.get('parts',[]))
    if isinstance(value,list):return '\n'.join(part for item in value if (part:=text_content(item)))
    return ''


def markdown_messages(text,names):
    labels={name.lower():'user' for name in ('user','human','me','用户','人类','我',names['user_name'])}
    labels.update({name.lower():'assistant' for name in ('assistant','ai','claude','chatgpt','gpt','deepseek','助手',names['ai_name'])})
    pattern=re.compile(r'^\s*(?:>\s*)?(?:[-*+]\s*)?(?:#{1,6}\s*)?(?:\*\*)?([^:：\n]+?)(?:\*\*)?\s*[:：]\s*(.*)$')
    result=[];role='user';parts=[];fenced=False
    def flush():
        content='\n'.join(parts)
        if content.strip():result.append({'role':role,'content':content})
    for line in text.splitlines():
        if line.lstrip().startswith(('```','~~~')):fenced=not fenced
        match=pattern.match(line) if not fenced else None
        label=match.group(1).strip().strip('*').lower() if match else ''
        if label in labels:
            flush();role=labels[label];parts=[match.group(2).removeprefix('**').lstrip()]
        else:parts.append(line)
    flush()
    return result


def chatgpt_messages(conversation):
    mapping=conversation['mapping']
    if not isinstance(mapping,dict):raise ValueError('ChatGPT mapping 必须是对象')
    head=conversation.get('current_node')
    if not head:
        parents={node.get('parent') for node in mapping.values() if isinstance(node,dict)}
        leaves=[key for key in mapping if key not in parents]
        if len(leaves)!=1:raise ValueError('ChatGPT 有多个分支但没有 current_node，无法确定要导入的对话分支')
        head=leaves[0]
    chain=[];seen=set()
    while head:
        if head in seen or head not in mapping:raise ValueError('ChatGPT 对话分支损坏')
        seen.add(head);node=mapping[head]
        if node.get('message'):chain.append({**node['message'],'id':node['message'].get('id') or head})
        head=node.get('parent')
    return list(reversed(chain))


def parse_file(content,filename,names,mode='auto',stamp=None):
    stamp=stamp or now();source_hash=digest(content);warnings=[]
    extension=Path(filename).suffix.lower()
    stripped=content.lstrip('\ufeff \r\n\t')
    if extension=='.jsonl':
        try:data=[json.loads(line) for line in stripped.splitlines() if line.strip()]
        except ValueError as exc:raise ValueError('JSONL 格式错误：'+str(exc)) from None
    elif extension=='.json' or stripped.startswith(('{','[')):
        try:data=json.loads(stripped)
        except ValueError as exc:raise ValueError('JSON 格式错误：'+str(exc)) from None
    elif extension in ('.md','.txt',''):
        data={'messages':markdown_messages(content,names)}
    else:raise ValueError('支持 .json、.jsonl、.md、.txt 文件')
    is_operit=isinstance(data,dict) and isinstance(data.get('memories'),list) and (
        'exportDate' in data or 'links' in data or any(isinstance(row,dict) and 'uuid' in row for row in data['memories']))
    if mode=='operit' and not is_operit:raise ValueError('文件不是 Operit 记忆备份')
    if is_operit:
        if mode=='conversation':raise ValueError('这是 Operit 记忆库，请选择自动识别或 Operit')
        entries=[]
        for index,row in enumerate(data['memories']):
            if not isinstance(row,dict) or not isinstance(row.get('content'),str) or not row['content'].strip():
                raise ValueError(f'Operit 第 {index+1} 条缺少正文，未开始导入')
            uuid=str(row.get('uuid') or '').strip()
            key='operit_'+digest(uuid or encode([row.get('title'),row['content'],row.get('createdAt')]))[:32]
            created=timestamp(row.get('createdAt'),stamp)
            entries.append({'id':key,'uuid':uuid,'title':str(row.get('title') or '').strip() or row['content'].strip()[:60],
                'body':row['content'],'created_at':created,'updated_at':timestamp(row.get('updatedAt'),created),'original':row})
        links=data.get('links',[])
        if links:warnings.append(f'保留备份中的 {len(links)} 条旧关联信息；它们不会直接变成已审核的 Scene 关系。')
        return {'format':'operit','entries':entries,'warnings':warnings,'links':links,'export_date':data.get('exportDate'),'source_hash':source_hash}
    sillytavern_header=None
    if isinstance(data,list) and data and isinstance(data[0],dict) and 'mes' not in data[0] and (
            'chat_metadata' in data[0] or {'user_name','character_name'}<=data[0].keys()):
        sillytavern_header=data[0]
        metadata=sillytavern_header.get('chat_metadata',{})
        if not isinstance(metadata,dict):raise ValueError('SillyTavern chat_metadata 必须是对象')
        integrity=metadata.get('integrity');created=sillytavern_header.get('create_date')
        session_key=['integrity',integrity] if integrity else ['create_date',created,
            sillytavern_header.get('user_name'),sillytavern_header.get('character_name')] if created else None
        if session_key:
            session='sillytavern-'+digest(encode(session_key))[:32]
        else:
            session='sillytavern-file-'+digest(encode(data))[:32]
            warnings.append('SillyTavern 缺少 integrity / create_date，会话标识由完整导出生成；裁剪或追加后无法跨文件自动去重。')
        data=[{'id':session,'messages':data[1:],'sillytavern':True}]
    if isinstance(data,dict) and 'conversations' in data:data=data['conversations']
    if isinstance(data,dict) and ('role' in data or 'sender' in data):data=[data]
    if isinstance(data,list) and data and all(isinstance(row,dict) and ('role' in row or 'sender' in row) for row in data):
        # JSONL may carry multiple sessions; preserve their explicit separation.
        groups={}
        for row in data:groups.setdefault(str(row.get('session_id') or row.get('conversation_id') or 'file'),[]).append(row)
        data=[{'messages':rows,**({'id':key} if key!='file' else {})} for key,rows in groups.items()]
    conversations=data if isinstance(data,list) else [data]
    entries=[];formats=set();skipped=0;missing_times=0
    for conversation in conversations:
        if not isinstance(conversation,dict):raise ValueError('对话条目必须是对象')
        if 'mapping' in conversation:messages=chatgpt_messages(conversation);fmt='chatgpt'
        elif 'chat_messages' in conversation:messages=conversation['chat_messages'];fmt='claude'
        elif 'messages' in conversation:messages=conversation['messages'];fmt='sillytavern' if conversation.get('sillytavern') else 'text' if extension in ('.txt','.md') else 'messages'
        elif 'events' in conversation:messages=conversation['events'];fmt='raw'
        else:raise ValueError('无法识别聊天记录，请提供 Claude、ChatGPT、SillyTavern、messages/events 或带角色的文本导出')
        if not isinstance(messages,list):raise ValueError('messages 必须是列表')
        formats.add(fmt)
        session=str(conversation.get('uuid') or conversation.get('id') or conversation.get('conversation_id') or
                    conversation.get('session_id') or 'file-'+digest(encode(messages))[:24])
        native_ids={};hidden=0;position_ids=0;colliding_times=0
        for index,message in enumerate(messages):
            if not isinstance(message,dict):raise ValueError('消息必须是对象')
            author=message.get('author',{});author=author if isinstance(author,dict) else {}
            if fmt=='sillytavern':
                if not isinstance(message.get('is_user'),bool) or not isinstance(message.get('mes'),str):
                    raise ValueError(f'SillyTavern 第 {index+1} 条消息需要布尔 is_user 和字符串 mes，未开始导入')
                # is_system also marks hidden dialogue; it is not a role.
                role='user' if message['is_user'] else 'assistant'
            else:role=str(message.get('sender') or message.get('role') or author.get('role') or '').lower()
            role={'human':'user','ai':'assistant','bot':'assistant'}.get(role,role)
            if role not in ('user','assistant'):skipped+=1;continue
            value=message['mes'] if fmt=='sillytavern' else message.get('text',message.get('content',''));text=text_content(value)
            if not text.strip():skipped+=1;continue
            original_time=message.get('send_date') if fmt=='sillytavern' else message.get('created_at',message.get('create_time',message.get('timestamp',message.get('time'))))
            if original_time in (None,''):missing_times+=1
            created=timestamp(original_time,stamp)
            message_id=message.get('uuid') or message.get('id') or message.get('source_event_id') or message.get('message_id')
            if fmt=='sillytavern':
                hidden+=message.get('is_system') is True
                if not message_id and original_time not in (None,''):
                    key=encode([created,role]);ordinal=native_ids.get(key,0);native_ids[key]=ordinal+1
                    message_id='send-date:'+key+':'+str(ordinal)
                    colliding_times+=ordinal>0
                elif not message_id:position_ids+=1
            message_id=str(message_id or f'position-{index}')
            entries.append({'source':'import-'+fmt,'source_event_id':digest(encode([session,message_id])),
                'session_id':session,'conversation_id':session,'role':role,'text':text,
                'created_at':created,'metadata':{'original_message_id':message_id,
                    'original_timestamp':original_time,'timestamp_source':'export' if original_time not in (None,'') else 'import_time',
                    'original_message':message,'conversation_title':conversation.get('name',conversation.get('title','')),
                    'source_file':Path(filename).name}})
        if hidden:warnings.append(f'{hidden} 条 SillyTavern 隐藏消息按 is_user 保留为原话；is_system 不作为系统角色过滤。')
        if position_ids:warnings.append(f'{position_ids} 条 SillyTavern 消息缺少消息 ID 和 send_date，按会话内位置识别；不要裁剪或重排这些消息后期待跨文件去重。')
        if colliding_times:warnings.append('SillyTavern 同一角色有多条消息的 send_date 相同，按出现顺序区分；分段时请将这些同时间消息留在同一段，保留完整顺序。')
    if skipped:warnings.append(f'{skipped} 条系统、工具或无文字消息未作为对话导入；文字消息中的原始结构保留在元数据里。')
    if missing_times:warnings.append(f'{missing_times} 条消息没有时间，使用首次导入时间归档，并标注“原始时间未知”。')
    return {'format':'+'.join(sorted(formats)),'entries':entries,'warnings':warnings,'source_hash':source_hash,
            **({'sillytavern_header':sillytavern_header} if sillytavern_header is not None else {})}


class ImportArchive(RawEventStore):
    def _normalize_event(self,raw,*,default_source,ingested_at):
        event,error=super()._normalize_event(raw,default_source=default_source,ingested_at=ingested_at)
        if event:
            # Retain ordinary whitespace, but preserve the archive's existing
            # removal of injected client context. The exact export is metadata.
            if event['text']==raw['text'].strip():event['text']=raw['text']
            event['event_hash']=self._event_hash(**{key:event[key] for key in
                ('source','source_event_id','role','text','created_at','conversation_id','session_id')})
        return event,error


def initialize_imports(database):
    with Store(database) as store:
        store.conn.executescript('''
            CREATE TABLE IF NOT EXISTS file_imports (id TEXT PRIMARY KEY,filename TEXT NOT NULL,format TEXT NOT NULL,
                payload_json TEXT NOT NULL,cursor INTEGER NOT NULL DEFAULT 0,inserted INTEGER NOT NULL DEFAULT 0,
                duplicate INTEGER NOT NULL DEFAULT 0,errors_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,
                tagging INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS file_import_entries (
                upload_id TEXT NOT NULL REFERENCES file_imports(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,entry_json TEXT NOT NULL,
                PRIMARY KEY(upload_id,position));
            CREATE TABLE IF NOT EXISTS import_tag_jobs (document_id TEXT PRIMARY KEY,body_hash TEXT NOT NULL,upload_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS tagging_outbox (
                document_id TEXT PRIMARY KEY
            );
            CREATE TRIGGER IF NOT EXISTS tagging_revision_insert AFTER INSERT ON revisions BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id)
                SELECT NEW.document_id WHERE EXISTS (
                    SELECT 1 FROM documents WHERE id=NEW.document_id AND kind IN ('event','scene'));
            END;
            CREATE TRIGGER IF NOT EXISTS tagging_binding_insert AFTER INSERT ON evidence_bindings BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id)
                SELECT NEW.document_id WHERE EXISTS (
                    SELECT 1 FROM documents WHERE id=NEW.document_id AND kind IN ('event','scene'));
            END;
            CREATE TRIGGER IF NOT EXISTS tagging_binding_update AFTER UPDATE ON evidence_bindings BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id)
                SELECT NEW.document_id WHERE EXISTS (
                    SELECT 1 FROM documents WHERE id=NEW.document_id AND kind IN ('event','scene'));
            END;
            CREATE TRIGGER IF NOT EXISTS tagging_binding_delete AFTER DELETE ON evidence_bindings BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id)
                SELECT OLD.document_id WHERE EXISTS (
                    SELECT 1 FROM documents WHERE id=OLD.document_id AND kind IN ('event','scene'));
            END;
            CREATE TRIGGER IF NOT EXISTS tagging_document_lifecycle AFTER UPDATE OF lifecycle ON documents
            WHEN NEW.kind IN ('event','scene') BEGIN
                INSERT OR IGNORE INTO tagging_outbox(document_id) VALUES (NEW.id);
            END;
        ''')
        # Old uploads are split once, atomically, without changing their cursor,
        # errors, tagging choice or history boundary. New uploads start split.
        with store.transaction(immediate=True):
            columns={row[1] for row in store.conn.execute('PRAGMA table_info(file_imports)')}
            if 'total' not in columns:
                store.conn.execute('ALTER TABLE file_imports ADD COLUMN total INTEGER')
            for row in store.conn.execute('SELECT id FROM file_imports WHERE total IS NULL').fetchall():
                data=json.loads(store.conn.execute('SELECT payload_json FROM file_imports WHERE id=?',(row['id'],)).fetchone()[0])
                save_entries(store,row['id'],data)
        marker=store.conn.execute("SELECT 1 FROM background_state WHERE name='tagging_outbox_seeded_v1'").fetchone()
        if not marker:
            store.conn.execute("INSERT OR IGNORE INTO tagging_outbox(document_id) "
                "SELECT id FROM documents WHERE kind IN ('event','scene') AND lifecycle='active'")
            store.conn.execute("INSERT INTO background_state(name,value_json) VALUES ('tagging_outbox_seeded_v1','{\"status\":\"done\"}')")
        archive_imported_originals(store.conn)


def save_entries(store,identifier,data):
    """Keep the small report separate from the ordered, durable import input."""
    entries=data['entries']
    summary={key:value for key,value in data.items() if key!='entries'}
    summary.update(total=len(entries),sessions=len({item['session_id'] for item in entries}) if data['format']!='operit' else 0,
        preview=[{'role':item.get('role'),'title':item.get('title'),
                  'text':item.get('text',item.get('body',''))[:500]} for item in entries[:3]])
    store.conn.executemany('INSERT INTO file_import_entries(upload_id,position,entry_json) VALUES (?,?,?)',
        ((identifier,index,encode(item)) for index,item in enumerate(entries)))
    store.conn.execute('UPDATE file_imports SET total=?,payload_json=? WHERE id=?',
                       (len(entries),encode(summary),identifier))


def archive_imported_originals(conn, upload_id=None):
    """Keep imported-history exclusion as a compact, reversible upload boundary."""
    conn.execute('CREATE TABLE IF NOT EXISTS raw_processing(raw_id INTEGER PRIMARY KEY,operation_id TEXT NOT NULL,outcome TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS pipeline_import_boundaries('
                 'upload_id TEXT PRIMARY KEY,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                 'released INTEGER NOT NULL DEFAULT 0)')
    columns={row[1] for row in conn.execute('PRAGMA table_info(pipeline_import_boundaries)')}
    if 'released' not in columns:
        conn.execute('ALTER TABLE pipeline_import_boundaries ADD COLUMN released INTEGER NOT NULL DEFAULT 0')
    tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'file_imports','raw_events'}<=tables:return
    # Migrate the old per-message markers before removing them. One upload boundary
    # replaces hundreds or thousands of archived_only rows and can later be released
    # without rewriting the raw archive.
    conn.execute("""INSERT OR IGNORE INTO pipeline_import_boundaries(upload_id)
        SELECT DISTINCT substr(operation_id,13) FROM raw_processing
        WHERE outcome='archived_only' AND operation_id LIKE 'file-import:%'""")
    conn.execute("""DELETE FROM raw_processing
        WHERE outcome='archived_only' AND operation_id LIKE 'file-import:%'""")
    if upload_id:
        conn.execute("""INSERT OR IGNORE INTO pipeline_import_boundaries(upload_id,released)
            SELECT ?,0 WHERE EXISTS (
                SELECT 1 FROM raw_events WHERE json_extract(metadata_json,'$.import_upload_id')=?)""",
            (str(upload_id),str(upload_id)))


def release_imported_originals(database, upload_id, *, skip=False):
    """Choose whether a completed conversation import enters Event processing."""
    identifier=str(upload_id or '').strip()
    if not identifier:
        raise ValueError('导入任务 ID 不能为空')
    initialize_imports(database)
    with Store(database) as store,store.transaction(immediate=True):
        row=store.conn.execute('SELECT id,format,cursor,total FROM file_imports WHERE id=?',(identifier,)).fetchone()
        if row is None:
            raise ValueError('找不到导入任务')
        if row['format']=='operit':
            raise ValueError('Operit 记忆导入不属于原话 Event 整理')
        if int(row['cursor'])!=int(row['total']):
            raise ValueError('请先完成这份聊天记录的导入')
        originals=store.conn.execute("SELECT count(*) FROM raw_events WHERE json_extract(metadata_json,'$.import_upload_id')=?",(identifier,)).fetchone()[0]
        if skip:
            boundary=store.conn.execute('SELECT released FROM pipeline_import_boundaries WHERE upload_id=?',(identifier,)).fetchone()
            if originals and (boundary is None or boundary['released']):
                raise ValueError('这份原话已加入整理，不能再跳过；请在摘要配置中查看任务。')
            store.conn.execute("INSERT INTO background_state(name,value_json) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",
                               ('import-choice:'+identifier,json.dumps({'choice':'skip'})))
            return {'status':'skipped','upload_id':identifier,'originals':int(originals or 0)}
        store.conn.execute('DELETE FROM background_state WHERE name=?',('import-choice:'+identifier,))
        released=store.conn.execute('UPDATE pipeline_import_boundaries SET released=1 WHERE upload_id=? AND released=0',(identifier,)).rowcount
    return {'status':'released' if released else 'unchanged','upload_id':identifier,
            'originals':int(originals or 0),'released':bool(released)}


def report(row):
    data=json.loads(row['payload_json']);total=row['total'];cursor=row['cursor'];errors=json.loads(row['errors_json'])
    return {'id':row['id'],'filename':row['filename'],'format':row['format'],'total':total,'processed':cursor,
        'inserted':row['inserted'],'duplicate':row['duplicate'],'failed':len(errors),'errors':errors[:20],
        'status':'completed' if cursor==total else 'ready' if not cursor else 'paused',
        'warnings':data['warnings'],'tagging':bool(row['tagging']),
        'sessions':data['sessions'],'preview':data['preview']}


def stage(database,content,filename,mode,tagging):
    initialize_imports(database)
    key='upload:'+digest(content)
    with Store(database) as store:
        row=store.conn.execute('SELECT * FROM file_imports WHERE id=?',(key,)).fetchone()
    if row:
        if (mode=='operit' and row['format']!='operit') or (mode=='conversation' and row['format']=='operit'):
            raise ValueError('已识别的文件格式与所选类型不同')
        return report(row)
    data=parse_file(content,filename,identity(database),mode)
    if not data['entries']:raise ValueError('文件中没有可导入的文字条目')
    with Store(database) as store,store.transaction(immediate=True):
        inserted=store.conn.execute('INSERT OR IGNORE INTO file_imports (id,filename,format,payload_json,created_at,tagging) VALUES (?,?,?,?,?,?)',
            (key,Path(filename).name,data['format'],'{}',now(),int(tagging and data['format']=='operit'))).rowcount
        if inserted:save_entries(store,key,data)
        return report(store.conn.execute('SELECT * FROM file_imports WHERE id=?',(key,)).fetchone())


def import_scene(store,item,upload,data):
    old=store.read(item['id'])
    if old:
        if old['body_md']!=item['body']:raise Conflict('同一 Operit UUID 的正文不同，未覆盖现有记忆')
        return 'duplicate'
    meta={'object_kind':'scene','memory_value_source':'imported_original','import_format':'operit','operit_uuid':item['uuid'],
        'operit_original':item['original'],'import_source_hash':data['source_hash'],'import_source_file':upload['filename'],
        'operit_export_date':data.get('export_date'),'canonical_domain':'general','domain':['general'],
        'scene_cues':[],
        'date':datetime.fromisoformat(item['created_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
        'scene_status':'active','active':True}
    store.create(item['id'],'scene',item['title'],item['body'],metadata=meta,
        created_at=item['created_at'],updated_at=item['updated_at'])
    source=store.add_source(encode(['operit_memory',item['uuid'] or item['id']]),item['body'],
        metadata={'type':'operit_memory','item_id':item['uuid'],'source_file':upload['filename'],
                  'source_system':'operit','message_id':item['uuid'] or item['id'],'session_id':'',
                  'created_at':item['created_at'],'original':item['original']})
    store.bind(item['id'],source,actor='import')
    store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)',(item['id'],))
    if upload['tagging']:store.conn.execute('INSERT OR IGNORE INTO import_tag_jobs(document_id,body_hash,upload_id) VALUES (?,?,?)',
        (item['id'],digest(item['body']),upload['id']))
    return 'inserted'


def import_originals(store,archive,batch,identifier,start,errors,counts):
    normalized=[];stamp=now()
    for offset,item in enumerate(batch):
        try:
            item={**item,'metadata':{**item['metadata'],'import_upload_id':identifier}}
            event,error=archive._normalize_event(item,default_source=item['source'],ingested_at=stamp)
            if event is None:raise ValueError('原话不符合归档要求：'+error)
            normalized.append((offset,event))
        except (ValueError,KeyError) as exc:
            errors.append({'entry':start+offset+1,'message':str(exc)[:250]})
    # One indexed query per source, rather than a connection/query per message.
    sources={}
    for _,event in normalized:sources.setdefault(event['source'],[]).append(event['source_event_id'])
    existing={}
    for source,keys in sources.items():
        placeholders=','.join('?' for _ in keys)
        for old in store.conn.execute('SELECT source,source_event_id,text,role FROM raw_events '
                f"WHERE source=? AND source_event_id != '' AND source_event_id IN ({placeholders})",(source,*keys)):
            existing[(old['source'],old['source_event_id'])]=old
    for offset,event in normalized:
        key=(event['source'],event['source_event_id']);old=existing.get(key)
        if old and (old['text']!=event['text'] or old['role']!=event['role']):
            errors.append({'entry':start+offset+1,'message':'同一原始消息 ID 的内容不同，未覆盖已存原话'})
            continue
        if old:status='duplicate'
        else:
            status,_=archive._insert_event_on_connection(store.conn,event)
            if status=='inserted':existing[key]=event
        counts[status]+=1


def advance_import(settings,identifier,*,batch_size=DEFAULT_BATCH_SIZE):
    if type(batch_size) is not int or not 1<=batch_size<=LARGE_BATCH_SIZE:
        raise ValueError(f'batch_size must be 1..{LARGE_BATCH_SIZE}')
    initialize_imports(settings.database)
    # Schema setup is outside the write transaction; row inserts below reuse it.
    archive=ImportArchive({'raw_events':{'db_path':str(settings.database)}})
    with Store(settings.database) as store,store.transaction(immediate=True):
        row=store.conn.execute('SELECT * FROM file_imports WHERE id=?',(identifier,)).fetchone()
        if row is None:raise ValueError('找不到导入任务')
        data=json.loads(row['payload_json']);start=row['cursor'];errors=json.loads(row['errors_json'])
        counts={'inserted':0,'duplicate':0};batch=[];chars=0
        for entry in store.conn.execute('SELECT position,entry_json FROM file_import_entries '
                'WHERE upload_id=? AND position>=? ORDER BY position LIMIT ?', (identifier,start,batch_size)):
            if entry['position']!=start+len(batch):raise ValueError('导入条目缺失，未推进进度')
            size=len(entry['entry_json'])
            if batch and chars+size>MAX_BATCH_CHARS:break
            batch.append(json.loads(entry['entry_json']));chars+=size
        if start<row['total'] and not batch:raise ValueError('导入条目缺失，未推进进度')
        if row['format']!='operit':
            import_originals(store,archive,batch,identifier,start,errors,counts)
            if counts['inserted']:
                store.conn.execute('INSERT OR IGNORE INTO pipeline_import_boundaries(upload_id) VALUES (?)',(identifier,))
        else:
            for offset,item in enumerate(batch):
                try:
                    with store.transaction():
                        status=import_scene(store,item,row,data)
                    counts[status]+=1
                except (ValueError,KeyError) as exc:
                    errors.append({'entry':start+offset+1,'message':str(exc)[:250]})
            from .compat.scenes import map_evidence_ids
            map_evidence_ids(store,[item['id'] for item in batch if 'id' in item])
            from .recall.passage_layouts import prepare_layouts
            prepare_layouts(settings,[item['id'] for item in batch if 'id' in item],store=store)
        store.conn.execute('UPDATE file_imports SET cursor=?,inserted=inserted+?,duplicate=duplicate+?,errors_json=? WHERE id=?',
            (start+len(batch),counts['inserted'],counts['duplicate'],encode(errors),identifier))
        return {**report(store.conn.execute('SELECT * FROM file_imports WHERE id=?',(identifier,)).fetchone()),
                'batch_processed':len(batch)}


def list_imports(database):
    initialize_imports(database)
    with Store(database,read_only=True) as store:
        return [report(row) for row in store.conn.execute('SELECT * FROM file_imports ORDER BY created_at DESC,rowid DESC LIMIT 10')]
