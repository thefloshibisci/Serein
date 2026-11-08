"""Explicit window writing and latest-window continuity; no model is called here."""
import json

from ..core.store import Store, Conflict, encode, digest, now

WRITING_RULE = ('准备换窗时，由正在聊天的主模型写这一窗：我眼中的你、我眼中的自己、这一窗发生的事。'
    '写具体的新认识、变化、选择和未完事项；没有变化可留空，不把暂时情绪写成固定人格。'
    '最近的事可引用已有 Scene，不自动把窗影拆成 Scene。新窗口只读最新窗影，不重新生成画像。')


def latest_shadow(store):
    return store.conn.execute("SELECT * FROM historical_works WHERE kind='shadow' "
        "AND id NOT IN (SELECT document_id FROM deletions) "
        "AND COALESCE(json_extract(metadata_json,'$.continuity_eligible'),1)!=0 "
        "ORDER BY json_extract(metadata_json,'$.created_at') DESC,id DESC LIMIT 1").fetchone()


def sync_introductions(store, sections):
    from ..deployment import read_from_store
    state = read_from_store(store)
    for field, section in [('user_description','user_view'),('ai_description','self_view')]:
        if sections.get(section):
            state['identity'][field] = sections[section]
    store.conn.execute("INSERT INTO background_state(name,value_json) VALUES ('deployment_settings',?) "
        "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json", (encode(state),))


class WindowShadows:
    def __init__(self, database):
        self.database = database

    def write(self, window_id: str, title: str, content: str = '', scene_ids: list[str] | None = None,
              expected_revision: int = 0, user_view: str | None = None,
              self_view: str | None = None, recent_events: str | None = None):
        """Write before handoff: user_view=我眼中的你, self_view=我眼中的自己, recent_events=这一窗发生的事.
        The latest saved window updates editable introductions. Empty views leave introductions unchanged.
        Reuse window_id/revision on retries. This never generates Scenes or a separate portrait.
        """
        if not window_id.strip() or len(window_id)>200 or not title.strip():
            raise ValueError('Window ID and title are required')
        sections = {'user_view':user_view, 'self_view':self_view, 'recent_events':recent_events}
        structured = any(value is not None for value in sections.values())
        sections = {key:(value or '').strip() for key,value in sections.items()}
        if any(len(sections[key])>2000 for key in ('user_view','self_view')) or len(sections['recent_events'])>20000:
            raise ValueError('Views allow 2000 characters each; recent events allow 20000')
        if structured:
            content = '\n\n'.join('## '+label+'\n\n'+sections[key] for key,label in
                [('user_view','我眼中的你'),('self_view','我眼中的自己'),('recent_events','这一窗发生的事')]
                if sections[key])
        if not content.strip():
            raise ValueError('Write at least one window section')
        key = 'shadow:'+window_id
        scene_ids = list(dict.fromkeys(scene_ids or []))
        with Store(self.database) as store, store.transaction(immediate=True):
            for scene in scene_ids:
                if not store.conn.execute("SELECT 1 FROM documents WHERE id=? AND kind='scene' AND lifecycle='active'",(scene,)).fetchone():
                    raise ValueError('Window shadow references an unavailable Scene')
            old = store.conn.execute("SELECT * FROM historical_works WHERE id=?",(key,)).fetchone()
            if store.conn.execute('SELECT 1 FROM deletions WHERE document_id=?',(key,)).fetchone():
                raise Conflict('A deleted window shadow cannot be overwritten')
            meta = json.loads(old['metadata_json']) if old else {}
            if old and old['title']==title and old['body_md']==content and meta.get('moment_bucket_ids_json')==encode(scene_ids) and meta.get('sections_json')==encode(sections if structured else {}):
                return {'id':key,'window_id':window_id,'revision':old['revision'],'status':'unchanged','ordinary_recall':False}
            if (old['revision'] if old else 0)!=expected_revision:
                raise Conflict('Read the current window shadow revision before editing')
            revision=expected_revision+1
            meta.update(window_id=window_id,revision_root_id=window_id,revision=revision,title=title,
                created_at=meta.get('created_at') or now(),updated_at=now(),
                sections_json=encode(sections if structured else {}),moment_bucket_ids_json=encode(scene_ids))
            store.conn.execute("INSERT INTO historical_works (id,kind,revision,title,body_md,body_sha256,metadata_json,origin,source_path) "
                "VALUES (?,'shadow',?,?,?,?,?,'authored','') ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,"
                "title=excluded.title,body_md=excluded.body_md,body_sha256=excluded.body_sha256,metadata_json=excluded.metadata_json",
                (key,revision,title,content,digest(content),encode(meta)))
            latest = latest_shadow(store)
            if structured and latest is not None and latest['id']==key:
                sync_introductions(store,sections)
        return {'id':key,'window_id':window_id,'revision':revision,'status':'saved','ordinary_recall':False}

    def read(self, window_id: str = ''):
        """Read the shadow's full content once, with section headings preserved. Omit window_id for the latest; explicit IDs are for history/editing."""
        with Store(self.database,read_only=True) as store:
            row = store.conn.execute("SELECT * FROM historical_works WHERE kind='shadow' AND id IN (?,?) "
                "AND id NOT IN (SELECT document_id FROM deletions)",(window_id,'shadow:'+window_id)).fetchone() if window_id else latest_shadow(store)
            if row is None:return {'status':'not_found'}
            meta=json.loads(row['metadata_json'])
            return {'status':'ok','id':row['id'],'window_id':meta.get('window_id'), 'title':row['title'],
                'content':row['body_md'],'revision':row['revision'],
                'scene_ids':json.loads(meta.get('moment_bucket_ids_json') or '[]'),'ordinary_recall':False,
                'writing_rule':WRITING_RULE}
