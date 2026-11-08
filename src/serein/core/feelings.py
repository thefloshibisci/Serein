"""Independent authored feelings; triggers are versioned references, never evidence."""

from datetime import datetime, timezone
import math
from uuid import uuid4

from .store import Store, Conflict, now


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be an ISO string with timezone')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp requires an explicit timezone')
    return value


def unit_number(value, name):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError(f'{name} must be a finite number in [0,1] or unknown')
    return value


def access_status(store, doc):
    if not doc or doc['lifecycle'] == 'deleted' or store.conn.execute(
            'SELECT 1 FROM deletions WHERE document_id=?', (doc['id'],)).fetchone():
        return 'unavailable'
    meta = doc['metadata']
    if meta.get('locked') or meta.get('visibility') in ('locked', 'deleted'):
        return 'restricted'
    if meta.get('unlock_at'):
        try:
            unlock = datetime.fromisoformat(timestamp(meta['unlock_at']).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return 'restricted'
        if unlock > datetime.now(timezone.utc):
            return 'restricted'
    return 'available'


def resolve_trigger(store, ref):
    """Read the exact frozen source only after checking its current access."""
    kind, key = ref.get('kind'), ref.get('id')
    public = {k: ref[k] for k in ('kind', 'id', 'revision', 'sha256', 'binding_id') if k in ref}
    if kind in ('event', 'scene'):
        current = store.read(key)
        status = access_status(store, current)
        if status != 'available':
            return {**public, 'status': status}
        if current['kind'] != kind or current['metadata'].get('memory_role') == 'feel':
            return {**public, 'status': 'invalid_source_kind'}
        old = store.read(key, revision=ref.get('revision'))
        if not old or old['body_sha256'] != ref.get('sha256'):
            return {**public, 'status': 'snapshot_mismatch'}
        if access_status(store, old) != 'available':
            return {**public, 'status': 'restricted'}
        return {**public, 'status': 'available', 'title': old['title'],
                'summary': old['body_md'][:240], 'current_revision': current['revision'],
                'current_lifecycle': current['lifecycle']}
    if kind == 'source':
        # Only a still-active binding to a readable memory authorizes this raw
        # snapshot. A detached source row is not a backdoor to deleted evidence.
        row = store.conn.execute('SELECT s.*,b.document_id,b.active FROM sources s '
            'JOIN evidence_bindings b ON b.source_id=s.id WHERE s.id=? AND b.id=?',
            (key, ref.get('binding_id'))).fetchone()
        if not row or not row['active'] or access_status(store, store.read(row['document_id'])) != 'available':
            return {**public, 'status': 'unavailable'}
        if row['content_sha256'] != ref.get('sha256') or ref.get('revision') != 1:
            return {**public, 'status': 'snapshot_mismatch'}
        return {**public, 'status': 'available', 'summary': row['content'][:240],
                'owner_id': row['document_id']}
    return {**public, 'status': 'invalid_source_kind'}


def freeze_triggers(store, references):
    if not isinstance(references, list) or not 1 <= len(references) <= 20:
        raise ValueError('Feel requires 1..20 verified source references')
    result = []
    for ref in references:
        if not isinstance(ref, dict) or not isinstance(ref.get('id'), str):
            raise ValueError('Invalid source reference')
        kind, key = ref.get('kind'), ref['id']
        if kind in ('event', 'scene'):
            doc = store.read(key, revision=ref.get('revision'))
            if not doc:
                raise ValueError('Source does not exist')
            frozen = {'kind': kind, 'id': key, 'revision': doc['body_revision'], 'sha256': doc['body_sha256']}
        elif kind == 'source':
            row = store.conn.execute('SELECT content_sha256 FROM sources WHERE id=?', (key,)).fetchone()
            if not row:
                raise ValueError('Source does not exist')
            frozen = {'kind': kind, 'id': key, 'revision': 1, 'sha256': row[0], 'binding_id': ref.get('binding_id')}
        else:
            raise ValueError('Sources must be Event, Scene or an authorized original binding')
        if ref.get('sha256') is not None and ref['sha256'] != frozen['sha256']:
            raise Conflict('Source content hash changed')
        if resolve_trigger(store, frozen)['status'] != 'available':
            raise ValueError('Source is not readable or has the wrong kind')
        if frozen not in result:
            result.append(frozen)
    return result


def source_views(store, doc):
    return [resolve_trigger(store, ref) for ref in doc['metadata'].get('feel_sources', [])]


def surface_reasons(store, doc):
    reasons = []
    meta = doc['metadata']
    for flag in ('dont_surface', 'digested'):
        if meta.get(flag):
            reasons.append(flag)
    views = source_views(store, doc)
    if not views or any(ref['status'] != 'available' for ref in views):
        reasons.append('feel_source_unavailable')
    return reasons


def backlinks(store, key):
    rows = store.conn.execute(
        "SELECT d.id,r.title,d.created_at FROM documents d JOIN revisions r "
        "ON r.document_id=d.id AND r.number=d.revision WHERE d.lifecycle!='deleted' "
        "AND json_extract(r.metadata_json,'$.memory_role')='feel' AND EXISTS "
        "(SELECT 1 FROM json_each(r.metadata_json,'$.feel_sources') s WHERE json_extract(s.value,'$.id')=?) "
        'ORDER BY d.created_at,d.id', (key,))
    return [dict(row) for row in rows if access_status(store, store.read(row['id'])) == 'available']


class Feelings:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def create_in_store(store, content, sources, *, author, title='', authored_at=None,
                        valence=None, arousal=None, can_surface=False, legacy=None,
                        document_id=None, lifecycle='active', flags=None):
        if not isinstance(content, str) or not content.strip() or len(content) > 100000:
            raise ValueError('Feel requires 1..100000 characters')
        if not isinstance(author, str) or not author.strip():
            raise ValueError('Feel requires an author')
        if can_surface is not None and type(can_surface) is not bool:
            raise ValueError('can_surface must be boolean or unreviewed')
        if lifecycle not in ('active', 'archived'):
            raise ValueError('Unsupported imported feel lifecycle')
        if flags and (set(flags) - {'digested', 'dont_surface', 'pinned', 'protected'}
                      or any(type(value) is not bool for value in flags.values())):
            raise ValueError('Invalid legacy feeling flags')
        frozen = freeze_triggers(store, sources)
        authored = timestamp(authored_at) if authored_at else (None if legacy is not None else now())
        meta = {'memory_role': 'feel', 'object_kind': 'scene', 'memory_value_source': 'authored_feel',
                'write_contract': 'feel-v1', 'author': author, 'authored_at': authored,
                'occurred_at': None, 'feel_sources': frozen, 'canonical_domain': 'feel', 'domain': ['feel'],
                'valence': unit_number(valence, 'valence'), 'arousal': unit_number(arousal, 'arousal'),
                'scene_cues': [], **(flags or {})}
        if legacy is not None:
            meta['compatibility'] = legacy
            meta['imported_at'] = now()
        doc = store.create(document_id or 'scene_feel_' + uuid4().hex, 'scene', title or content[:40], content,
                           metadata=meta, manual_surface=can_surface, lifecycle=lifecycle, created_at=authored)
        store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', (doc['id'],))
        return doc

    def create(self, content, sources, **options):
        with Store(self.database) as store, store.transaction(immediate=True):
            doc = self.create_in_store(store, content, sources, **options)
        return self.read(doc['id'])

    def read(self, feel_id, revision=None):
        from .reader import Reader
        with Reader(self.database) as reader:
            return reader.read(feel_id, kind='feel', revision=revision, with_evidence=False)

    def edit(self, feel_id, expected_revision, *, content=None, can_surface=None, lifecycle=None):
        with Store(self.database) as store, store.transaction(immediate=True):
            doc = store.read(feel_id)
            if not doc or doc['metadata'].get('memory_role') != 'feel' or doc['revision'] != expected_revision:
                raise Conflict('Feel missing or revision changed')
            if access_status(store, doc) != 'available':
                raise ValueError('Feel is unavailable')
            if content is not None and (not isinstance(content, str) or not content.strip() or len(content) > 100000):
                raise ValueError('Invalid feel content')
            if lifecycle is not None and lifecycle not in ('active', 'archived', 'deleted'):
                raise ValueError('Invalid feel lifecycle')
            if can_surface is not None and type(can_surface) is not bool:
                raise ValueError('can_surface must be boolean')
            store.revise(feel_id, expected_revision=expected_revision, title=doc['title'],
                         body_md=doc['body_md'] if content is None else content)
            if can_surface is not None:
                store.set_manual_surface(feel_id, can_surface)
            if lifecycle is not None:
                store.set_lifecycle(feel_id, lifecycle)
            store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', (feel_id,))
        return self.read(feel_id)

    def search(self, query='', *, source_id='', date_from='', date_to='', limit=20, offset=0, include_archived=False):
        from .reader import Reader
        from datetime import date
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError('Invalid pagination')
        for value in (date_from, date_to):
            if value:
                date.fromisoformat(value)
        if date_from and date_to and date_from > date_to:
            raise ValueError('Invalid date range')
        with Reader(self.database) as reader:
            rows = reader.store.conn.execute(
                "SELECT d.id FROM documents d JOIN revisions r ON r.document_id=d.id AND r.number=d.revision "
                "WHERE json_extract(r.metadata_json,'$.memory_role')='feel' AND d.lifecycle IN ('active','archived') "
                "AND (? OR d.lifecycle='active') AND (instr(lower(r.body_md||' '||r.title),lower(?))>0) "
                "AND (?='' OR EXISTS(SELECT 1 FROM json_each(r.metadata_json,'$.feel_sources') s WHERE json_extract(s.value,'$.id')=?)) "
                "AND (?='' OR substr(json_extract(r.metadata_json,'$.authored_at'),1,10)>=?) "
                "AND (?='' OR substr(json_extract(r.metadata_json,'$.authored_at'),1,10)<=?) "
                'ORDER BY d.created_at DESC,d.id LIMIT ? OFFSET ?',
                (include_archived, query, source_id, source_id, date_from, date_from, date_to, date_to, limit + 1, offset)).fetchall()
            return {'method': 'literal', 'items': [reader.read(r[0], kind='feel', with_evidence=False) for r in rows[:limit]],
                    'has_more': len(rows) > limit, 'next_offset': offset + limit if len(rows) > limit else None}
