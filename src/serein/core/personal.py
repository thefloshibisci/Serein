"""Personal reading marks and human recall labels, separate from memory prose."""

import json
from uuid import uuid4
from .store import Store, Conflict, encode, now
from .reader import Reader

SCOPES = {'favorite', 'annotation', 'recall_review', 'recall_simulation', 'preference'}


class Personal:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def record(row):
        return {**dict(row), 'value': {} if row['deleted'] else json.loads(row['payload_json']),
                'payload_json': None}

    @staticmethod
    def validate(scope, key, value, document_id, *, deleted=False):
        if scope not in SCOPES or not isinstance(key,str) or not 1 <= len(key) <= 240:
            raise ValueError('Invalid personal record identity')
        if not isinstance(value,dict) or len(encode(value)) > 150000:
            raise ValueError('Invalid personal record value')
        if scope == 'preference':
            from .preferences import validate
            if document_id is not None:
                raise ValueError('喜好不绑定记忆 ID')
            return validate(value, deleting=deleted)
        if scope == 'favorite':
            if type(value.get('favorite')) is not bool or key != document_id:
                raise ValueError('Favorite requires its document ID and a boolean')
            return {'favorite':value['favorite']}
        if scope == 'annotation':
            if not str(value.get('content') or '').strip() or len(value['content']) > 20000:
                raise ValueError('Annotation requires 1..20000 characters')
            if value.get('role') not in ('user','assistant','ai','unknown'):
                raise ValueError('Annotation requires an author role')
            return {k:value.get(k,'') for k in ('content','author','role','createdAt')}
        if scope == 'recall_review':
            # Reviews hold judgments, never injected memory bodies.
            return {k:v for k,v in value.items() if k in
                    ('verdict','observedAt','query','updatedAt','candidateReviews','routeVerdict','expectedRoute')}
        if not value.get('query') or value.get('expectedAction') not in ('recall','skip'):
            raise ValueError('Simulation label requires a query and expected action')
        return value

    def save(self, scope, key, value, *, document_id=None, expected_revision=0, deleted=False):
        with Store(self.database) as store, store.transaction(immediate=True):
            return self.save_in_store(store, scope, key, value, document_id=document_id,
                                      expected_revision=expected_revision, deleted=deleted)

    @staticmethod
    def save_in_store(store, scope, key, value, *, document_id=None, expected_revision=0, deleted=False):
        """Save using the caller's transaction, including memory-write receipts."""
        value=Personal.validate(scope,key,value,document_id,deleted=deleted)
        if scope in ('favorite','annotation'):
            doc=store.read(document_id)
            if not doc or doc['lifecycle']=='deleted' or doc['kind'] not in ('scene','event','narrative'):
                raise ValueError('Memory is not available for personal marks')
        row=store.conn.execute('SELECT * FROM personal_records WHERE scope=? AND key=?',(scope,key)).fetchone()
        if row and row['document_id'] != document_id:
            raise Conflict('Personal record belongs to another memory')
        if row and row['payload_json']==encode(value) and bool(row['deleted'])==deleted:
            return Personal.record(row)
        revision=row['revision'] if row else 0
        if revision != expected_revision:
            raise Conflict('Personal record changed; reload before saving')
        stamp=now()
        store.conn.execute('INSERT INTO personal_records VALUES (?,?,?,?,?,?,?,?) '
            'ON CONFLICT(scope,key) DO UPDATE SET payload_json=excluded.payload_json,revision=excluded.revision,'
            'deleted=excluded.deleted,updated_at=excluded.updated_at',
            (scope,key,document_id,encode(value),revision+1,int(deleted),stamp,stamp))
        return Personal.record(store.conn.execute('SELECT * FROM personal_records WHERE scope=? AND key=?',(scope,key)).fetchone())

    def list(self, scope, *, offset=0, limit=500):
        if scope not in SCOPES or not 1<=limit<=500 or offset<0:raise ValueError('Invalid personal list options')
        with Store(self.database,read_only=True) as store:
            rows=store.conn.execute('SELECT p.* FROM personal_records p LEFT JOIN documents d ON d.id=p.document_id '
                "WHERE p.scope=? AND p.deleted=0 AND (p.document_id IS NULL OR d.lifecycle!='deleted') "
                'ORDER BY p.key LIMIT ? OFFSET ?',(scope,limit+1,offset)).fetchall()
            return {'items':[self.record(r) for r in rows[:limit]],'has_more':len(rows)>limit,'next_offset':offset+limit}

    def import_legacy(self, records):
        if not isinstance(records,list) or len(records)>200:raise ValueError('Import at most 200 personal records at once')
        inserted=0;skipped=[]
        with Store(self.database) as store, store.transaction(immediate=True):
            for item in records:
                scope,key=item['scope'],item['key'];document_id=item.get('document_id')
                value=self.validate(scope,key,item['value'],document_id)
                if scope in ('favorite','annotation'):
                    doc=store.read(document_id)
                    if not doc or doc['lifecycle']=='deleted':
                        skipped.append(key);continue
                # Existing values and deletion tombstones always win over stale browsers.
                stamp=now()
                result=store.conn.execute('INSERT OR IGNORE INTO personal_records VALUES (?,?,?,?,1,0,?,?)',
                    (scope,key,document_id,encode(value),stamp,stamp))
                inserted+=result.rowcount
        return {'status':'ok','inserted':inserted,'skipped':skipped}

    def read_favorites(self, limit=5, offset=0, include_archived=False, with_evidence=False, *, kinds=None):
        if not 1<=limit<=100 or offset<0:raise ValueError('limit must be 1..100; offset must be nonnegative')
        selected=kinds or ('scene','event','narrative')
        if any(kind not in ('scene','event','narrative') for kind in selected):raise ValueError('Invalid favorite memory kind')
        with Reader(self.database) as reader:
            rows=reader.store.conn.execute('SELECT p.document_id FROM personal_records p JOIN documents d ON d.id=p.document_id '
                "WHERE p.scope='favorite' AND p.deleted=0 AND json_extract(p.payload_json,'$.favorite')=1 "
                "AND d.lifecycle IN ('active','archived') AND (? OR d.lifecycle='active') "
                'AND d.kind IN ('+','.join('?' for _ in selected)+') '
                'ORDER BY p.updated_at DESC,p.key LIMIT ? OFFSET ?',(include_archived,*selected,limit+1,offset)).fetchall()
            items=[reader.read(r[0],with_evidence=with_evidence) for r in rows[:limit]]
            return {'items':items,'has_more':len(rows)>limit,'next_offset':offset+len(items),'injected':False}

    def annotate(self, memory_id:str, content:str, author:str='Assistant', role:str='assistant', annotation_id:str=''):
        """Append a separately authored annotation; never rewrite the memory or its evidence. Reuse annotation_id on retry."""
        return self.save('annotation',annotation_id or 'annotation_'+uuid4().hex,
            {'content':content,'author':author,'role':role,'createdAt':''},document_id=memory_id)
