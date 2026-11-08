"""Versioned intention ledger. Reminder expiry cannot settle a promise."""
import json
from uuid import uuid4

from .store import Store, Conflict, encode, now
from .feelings import freeze_triggers, resolve_trigger, timestamp, unit_number


def read_plan(store, plan_id, revision=None):
    table = 'authored_plans' if revision is None else 'authored_plan_revisions'
    key = 'id' if revision is None else 'plan_id'
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='authored_plans'").fetchone():
        return None
    row = store.conn.execute(f'SELECT payload_json FROM {table} WHERE {key}=?' +
                             (' AND revision=?' if revision is not None else ''),
                             (plan_id, revision) if revision is not None else (plan_id,)).fetchone()
    return json.loads(row[0]) if row else None


def public_plan(store, plan):
    if not plan:
        return None
    return {**plan, 'related': [resolve_trigger(store, ref) for ref in plan['related']],
            'semantics': 'intention_not_event_evidence', 'can_surface_as_fact': False}


def backlinks(store, key):
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='authored_plans'").fetchone():
        return []
    return [{'id': row['id'], 'status': json.loads(row['payload_json'])['status']}
            for row in store.conn.execute(
                "SELECT * FROM authored_plans p WHERE EXISTS(SELECT 1 FROM json_each(p.payload_json,'$.related') r "
                "WHERE json_extract(r.value,'$.id')=?) ORDER BY p.id", (key,))]


class Plans:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def create_in_store(store, content, *, actor, weight=0.5, related=None, authored_at=None,
                        status='active', legacy=None, plan_id=None):
        if not isinstance(content, str) or not content.strip() or len(content) > 100000 or not actor.strip():
            raise ValueError('Plan requires content and actor')
        if status not in ('active', 'resolved', 'abandoned'):
            raise ValueError('Unsupported plan status')
        if legacy is None and status != 'active':
            raise ValueError('New plans start active; settle through an explicit action')
        stamp = timestamp(authored_at) if authored_at else (None if legacy is not None else now())
        item = {'id': plan_id or 'plan_' + uuid4().hex, 'kind': 'plan', 'revision': 1,
                'content': content, 'original_content': content, 'actor': actor, 'status': status,
                'weight': unit_number(weight, 'weight'), 'related': freeze_triggers(store, related) if related else [],
                'authored_at': stamp, 'occurred_at': None, 'updated_at': now(), 'reminder_id': None,
                'history': [{'action': 'imported' if legacy else 'created', 'to': status, 'actor': actor, 'at': now()}]}
        if legacy is not None:
            item['compatibility'] = legacy
            item['imported_at'] = now()
            original = legacy.get('original', {})
            item['legacy_history'] = original.get('change_log', (original.get('metadata') or {}).get('change_log', []))
        store.conn.execute('INSERT INTO authored_plans VALUES (?,?,?)', (item['id'], 1, encode(item)))
        store.conn.execute('INSERT INTO authored_plan_revisions VALUES (?,?,?)', (item['id'], 1, encode(item)))
        return item

    def create(self, content, **options):
        with Store(self.database) as store, store.transaction(immediate=True):
            return public_plan(store, self.create_in_store(store, content, **options))

    def read(self, plan_id, revision=None):
        with Store(self.database, read_only=True) as store:
            return public_plan(store, read_plan(store, plan_id, revision))

    def list(self, *, status='', query='', limit=20, offset=0):
        if status not in ('', 'active', 'resolved', 'abandoned') or not 1 <= limit <= 100 or offset < 0:
            raise ValueError('Invalid plan filter')
        with Store(self.database, read_only=True) as store:
            if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='authored_plans'").fetchone():
                return {'items': [], 'has_more': False, 'next_offset': None}
            rows = store.conn.execute(
                "SELECT payload_json FROM authored_plans WHERE (?='' OR json_extract(payload_json,'$.status')=?) "
                "AND instr(lower(json_extract(payload_json,'$.content')),lower(?))>0 ORDER BY id LIMIT ? OFFSET ?",
                (status, status, query, limit + 1, offset)).fetchall()
            return {'items': [public_plan(store, json.loads(r[0])) for r in rows[:limit]],
                    'has_more': len(rows) > limit, 'next_offset': offset + limit if len(rows) > limit else None}

    def update(self, plan_id, expected_revision, *, actor, reason, status=None, content=None, reminder_id=None):
        if not actor.strip() or not reason.strip():
            raise ValueError('Explicit actor and reason are required')
        if status is not None and status not in ('active', 'resolved', 'abandoned'):
            raise ValueError('Invalid plan state')
        with Store(self.database) as store, store.transaction(immediate=True):
            old = read_plan(store, plan_id)
            if not old or old['revision'] != expected_revision:
                raise Conflict('Plan missing or revision changed')
            changes = {}
            if status is not None:
                changes['status'] = status
            if content is not None:
                if not isinstance(content, str) or not content.strip() or len(content) > 100000:
                    raise ValueError('Invalid plan content')
                changes['content'] = content
            if reminder_id is not None:
                if reminder_id:
                    exists = store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='reminders'").fetchone()
                    if not exists or not store.conn.execute('SELECT 1 FROM reminders WHERE id=?', (reminder_id,)).fetchone():
                        raise ValueError('Reminder not found')
                changes['reminder_id'] = reminder_id or None
            if all(old.get(k) == v for k, v in changes.items()):
                return public_plan(store, old)
            item = {**old, **changes, 'revision': expected_revision + 1, 'updated_at': now(),
                    'history': [*old['history'], {'action': 'explicit_update', 'actor': actor, 'reason': reason,
                                                'at': now(), 'from': old['status'], 'to': changes.get('status', old['status']),
                                                'fields': sorted(changes)}]}
            store.conn.execute('UPDATE authored_plans SET revision=?,payload_json=? WHERE id=?',
                               (item['revision'], encode(item), plan_id))
            store.conn.execute('INSERT INTO authored_plan_revisions VALUES (?,?,?)',
                               (plan_id, item['revision'], encode(item)))
            return public_plan(store, item)
