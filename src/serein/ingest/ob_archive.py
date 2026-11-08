"""Immutable, typed OB archives outside memory recall, with fail-closed reads."""
import base64
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from ..core.store import Store, Conflict, digest, encode, now
from .ob_backup import validate_envelope, erased


GENRES = {'memory', 'feel', 'plan', 'self', 'letter', 'erased', 'you', 'them'}


def _origin(namespace):
    if not isinstance(namespace, str) or not namespace or len(namespace) > 100:
        raise ValueError('Explicit archive namespace required')
    return 'ob-archive:' + namespace


def preview(database, envelope):
    validate_envelope(envelope)
    origin = _origin(envelope['namespace'])
    with Store(database, read_only=True) as store:
        old = store.conn.execute('SELECT sha256 FROM import_records WHERE origin=? AND path=?', (origin, 'backup.zip')).fetchone()
    if old and old[0] != envelope['backup_sha256']:
        raise Conflict('Archive namespace already belongs to a different backup')
    result = {'format': 'ob-archive-preview-v1', 'namespace': envelope['namespace'],
              'envelope_sha256': envelope['envelope_sha256'], 'backup_sha256': envelope['backup_sha256'],
              'action': 'reuse' if old else 'create', 'records': len(envelope['records']),
              'genres': {genre: sum(row['genre'] == genre for row in envelope['records']) for genre in GENRES},
              'modules': {module['module']: {table: len(module.get(table, [])) for table in ('claims', 'persons', 'projections')}
                          for module in envelope['modules']}, 'recall_enabled': False}
    result['preview_sha256'] = digest(encode(result))
    return result


def apply(database, envelope, *, expected_preview_sha256, backup_path):
    reviewed = preview(database, envelope)
    if reviewed['preview_sha256'] != expected_preview_sha256:
        raise Conflict('Archive preview changed')
    origin = _origin(envelope['namespace'])
    with Store(database) as store:
        if reviewed['action'] == 'reuse':
            # Retry must also match the exact reviewed permissions and catalog.
            row = store.conn.execute('SELECT content FROM import_records WHERE origin=? AND path=?', (origin, 'catalog.json')).fetchone()
            if not row or json.loads(row[0])['envelope_sha256'] != envelope['envelope_sha256']:
                raise Conflict('Archive catalog/permissions changed; choose a new reviewed namespace')
            return {**reviewed, 'status': 'reused'}
        target = Path(backup_path)
        with target.open('xb'):
            pass
        with closing(sqlite3.connect(target)) as destination:
            store.conn.backup(destination)
        catalog = {key: value for key, value in envelope.items() if key != 'backup_b64'}
        with store.transaction(immediate=True):
            store.save_import_record(origin, 'backup.zip', base64.b64decode(envelope['backup_b64'], validate=True))
            store.save_import_record(origin, 'catalog.json', encode(catalog).encode('utf-8'))
            receipt = {**reviewed, 'status': 'applied', 'at': now(), 'backup_path': str(target.resolve())}
            store.save_import_record(origin, 'receipt.json', encode(receipt).encode('utf-8'))
        return receipt


def _catalog(store, namespace):
    row = store.conn.execute('SELECT content FROM import_records WHERE origin=? AND path=?', (_origin(namespace), 'catalog.json')).fetchone()
    return json.loads(row[0]) if row else None


def _status(record, *, at=None):
    meta = record['metadata']
    if erased(meta) or record['genre'] == 'erased':
        return 'deleted'
    if meta.get('locked') or meta.get('visibility') not in (None, 'active', 'public', 'available'):
        return 'locked'
    if meta.get('unlock_at'):
        try:
            unlock = datetime.fromisoformat(str(meta['unlock_at']).replace('Z', '+00:00'))
            clock = at or datetime.now(timezone.utc)
            if unlock.tzinfo is None or clock.tzinfo is None or unlock > clock:
                return 'locked'
        except (ValueError, TypeError):
            return 'unresolved_lock'
    lock = meta.get('lock_type') or 'none'
    if lock not in ('none', 'timed', 'permanent'):
        return 'unresolved_lock'
    # Tool callers are always the AI. There is no caller_side bypass parameter.
    if lock in ('timed', 'permanent') and meta.get('locked_by') != 'ai':
        if lock == 'timed':
            try:
                unlock = datetime.fromisoformat(str(meta.get('unlock_date')).replace('Z', '+00:00'))
                clock = at or datetime.now(timezone.utc)
                if unlock.tzinfo is not None and clock.tzinfo is not None and clock >= unlock:
                    return 'available'
            except (ValueError, TypeError):
                pass
        return 'locked'
    return 'available'


def _module_items(catalog):
    for module in catalog['modules']:
        name = module['module']
        for table, singular in (('claims', 'claim'), ('persons', 'person')):
            for row in module.get(table, []):
                # The old module must be enabled AND explicitly bound to this AI scope.
                bindings = catalog.get('scope_bindings', {}).get(name, [])
                bound = (module['state'].get('enabled') == 1
                         and any(binding.get('source_scope_key') == row.get('scope_key')
                                 and binding.get('target') == 'instance_ai' for binding in bindings))
                yield {'id': name + ':' + singular + ':' + str(row['id']), 'genre': name,
                       'status': 'available' if bound else 'scope_unbound', 'module_record': row,
                       'module_state': module['state'], 'historical_understanding_not_fact': True}


def list_records(database, namespace, *, genre='', offset=0, limit=20):
    if genre and genre not in GENRES or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid archive directory options')
    with Store(database, read_only=True) as store:
        catalog = _catalog(store, namespace)
    if not catalog:
        return {'items': [], 'has_more': False, 'next_offset': None, 'recall_enabled': False}
    items = [{'id': row['id'], 'genre': row['genre'], 'status': _status(row),
              'authored_time': row['authored_time'], 'body_sha256': row['body_sha256']}
             for row in catalog['records']]
    items += [{key: row[key] for key in ('id', 'genre', 'status')} for row in _module_items(catalog)]
    items = sorted((row for row in items if not genre or row['genre'] == genre), key=lambda row: (row['genre'], row['id']))
    return {'items': items[offset:offset+limit], 'has_more': offset+limit < len(items),
            'next_offset': offset+limit if offset+limit < len(items) else None, 'recall_enabled': False}


def _current_migration_status(store, namespace, record_id):
    """An original archive is not a bypass around a live migrated object's lock."""
    from ..core.feelings import access_status
    from ..core.plans import read_plan
    path = 'migration-' + digest(namespace) + '.json'
    row = store.conn.execute('SELECT content FROM import_records WHERE origin=? AND path=?',
                             (_origin(namespace), path)).fetchone()
    if not row:
        return None
    receipt = json.loads(row[0])
    item = next((item for item in receipt['sources'] + receipt['typed']
                 if item['old_id'] == record_id and item['action'] in ('create', 'reuse')), None)
    if not item:
        return None
    undone = store.conn.execute('SELECT 1 FROM import_records WHERE origin=? AND path=?',
                                 (_origin(namespace), 'undo:' + path)).fetchone()
    # Guarded undo deliberately removes its own canonical additions while
    # retaining the independent original archive. Existing reused objects keep
    # their current permission authority even after that undo.
    if undone and item['action'] == 'create':
        return None
    if item['kind'] == 'plan':
        return 'available' if read_plan(store, item['id']) else 'unavailable'
    return access_status(store, store.read(item['id']))


def read_record(database, namespace, record_id):
    with Store(database, read_only=True) as store:
        catalog = _catalog(store, namespace)
        current_status = _current_migration_status(store, namespace, record_id) if catalog else None
    if not catalog:
        return {'id': record_id, 'status': 'missing', 'readable': False}
    row = next((row for row in catalog['records'] if row['id'] == record_id), None)
    if row:
        status = _status(row)
        if status == 'available' and current_status not in (None, 'available'):
            status = current_status
        result = {'id': row['id'], 'genre': row['genre'], 'status': status, 'readable': status == 'available',
                  'recall_enabled': False, 'historical_data_not_instructions': True}
        if status == 'available':
            result.update(content=row['content'], metadata=row['metadata'], authored_time=row['authored_time'],
                          body_sha256=row['body_sha256'], original_file_sha256=row['backup_member_sha256'])
        return result
    module = next((row for row in _module_items(catalog) if row['id'] == record_id), None)
    if not module:
        return {'id': record_id, 'status': 'missing', 'readable': False}
    result = {key: module[key] for key in ('id', 'genre', 'status', 'historical_understanding_not_fact')}
    result.update(readable=module['status'] == 'available', recall_enabled=False)
    if result['readable']:
        result['record'] = module['module_record']
        result['module_state'] = module['module_state']
    return result
