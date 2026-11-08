"""Reviewed OB backup migration: exact identity reuse, atomic local rehearsal."""
import base64
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from ..core.feelings import Feelings, access_status
from ..core.plans import Plans, read_plan
from ..core.store import Store, Conflict, digest, encode, now
from ..legacy_migration.scan import is_daily_impression
from . import ob_archive, ob_backup, ob_types


def _ordinary(record):
    meta = record['metadata']
    return (record['genre'] in ('memory', 'self') and not is_daily_impression(meta)
            and meta.get('type') != 'whisper' and 'whisper' not in (meta.get('domain') or [])
            and '__whisper__' not in (meta.get('tags') or []))


def _source_map(store):
    result = {}
    for row in store.conn.execute('SELECT d.id,r.metadata_json FROM documents d JOIN revisions r '
                                  "ON r.document_id=d.id AND r.number=d.revision WHERE d.kind='scene'"):
        meta = json.loads(row['metadata_json'])
        old_id = meta.get('legacy_id')
        if old_id:
            result.setdefault(old_id, []).append(row['id'])
    return result


def _execute(store, envelope):
    """Caller owns one transaction. No model, canonical pipeline or index runs."""
    existing = _source_map(store)
    mapping, source_items, typed_items, created = {}, [], [], []
    namespace = envelope['namespace']
    for record in envelope['records']:
        if not _ordinary(record):
            continue
        old_id, meta = record['id'], record['metadata']
        if (ob_archive._status(record) != 'available' or meta.get('unlock_at')
                or (meta.get('lock_type') or 'none') != 'none'):
            source_items.append({'old_id': old_id, 'action': 'review', 'reason': 'restricted_legacy_source_retained_only_in_archive'})
            continue
        matches = existing.get(old_id, [])
        if len(matches) > 1:
            source_items.append({'old_id': old_id, 'action': 'review', 'reason': 'multiple_existing_exact_id_matches'})
            continue
        if matches:
            document = store.read(matches[0])
            if document['body_sha256'] not in (record['body_sha256'], record['ob_reader_body_sha256']):
                source_items.append({'old_id': old_id, 'action': 'review', 'reason': 'exact_id_body_changed'})
                continue
            item = {'old_id': old_id, 'id': document['id'], 'action': 'reuse', 'kind': 'scene',
                    'revision': document['revision'], 'sha256': document['body_sha256'],
                    'match_basis': 'exact_id_raw_body_hash' if document['body_sha256'] == record['body_sha256']
                                   else 'exact_id_old_frontmatter_reader_body_hash'}
        else:
            if record['genre'] == 'self' and envelope.get('self_mappings', {}).get(old_id) != {'target': 'scene'}:
                source_items.append({'old_id': old_id, 'action': 'review', 'reason': 'new_self_scene_mapping_required'})
                continue
            key = 'scene_ob_backup_' + digest(encode([namespace, old_id]))
            if store.read(key) or store.conn.execute('SELECT 1 FROM deletions WHERE document_id=?', (key,)).fetchone():
                raise Conflict('Source target identity already exists without the expected legacy mapping')
            archived = record['backup_member'].startswith('buckets/archive/') or meta.get('type') in ('archived', 'archive')
            metadata = {'object_kind': 'scene', 'memory_value_source': 'authored_scene',
                        'write_contract': 'ob-backup-authored-preservation-v1', 'legacy_id': old_id,
                        'legacy_exact_body_sha256': record['body_sha256'], 'legacy_ombre_metadata': meta,
                        'legacy_flags': {key: meta[key] for key in ('pinned', 'protected', 'anchor', 'dont_surface', 'digested') if key in meta},
                        'canonical_domain': (meta.get('domain') or ['general'])[0] if isinstance(meta.get('domain'), list) else meta.get('domain') or 'general',
                        'scene_cues': [meta.get('title') or meta.get('name') or old_id],
                        'import_format': 'ob-backup-v1', 'provenance_kind': 'legacy_authored_memory_not_verified_event',
                        'authored_time': record['authored_time'], 'original_file_sha256': record['backup_member_sha256'],
                        'source_archive_namespace': namespace, 'source_archive_id': old_id}
            if record['genre'] == 'self':
                metadata['compatibility'] = {'source_system': 'ombre_brain', 'source_role': 'self_authored',
                    'old_id': old_id, 'mapping': {'target': 'scene'}, 'provenance': 'legacy_authored_self; not current Persona'}
            document = store.create(key, 'scene', str(meta.get('title') or meta.get('name') or 'OB 历史记忆'),
                record['content'], lifecycle='archived' if archived else 'active', manual_surface=False,
                metadata=metadata, created_at=envelope['prepared_at'], updated_at=envelope['prepared_at'])
            item = {'old_id': old_id, 'id': key, 'action': 'create', 'kind': 'scene',
                    'revision': document['revision'], 'sha256': document['body_sha256']}
            created.append({'id': key, 'kind': 'scene', 'sha256': document['body_sha256']})
        source_items.append(item)
        if access_status(store, document) == 'available':
            mapping[old_id] = {key: item[key] for key in ('kind', 'id', 'revision', 'sha256')}
    typed = [row for row in ob_backup.typed_records(envelope) if row['genre'] in ('feel', 'plan')]
    for start in range(0, len(typed), 200):
        batch = typed[start:start+200]
        reviewed = ob_types.preview_in_store(store, batch, mapping, namespace)
        for raw in batch:
            store.save_import_record('ob-types:' + namespace, raw['id'], encode(raw).encode())
        for item in reviewed['items']:
            typed_items.append(item)
            if item['action'] != 'create':
                continue
            if item['kind'] == 'feel':
                saved = Feelings.create_in_store(store, item['content'], item['references'], **item['options'])
                body_sha = saved['body_sha256']
            else:
                saved = Plans.create_in_store(store, item['content'], related=item['references'], **item['options'])
                body_sha = digest(item['content'])
            created.append({'id': item['id'], 'kind': item['kind'], 'sha256': body_sha})
    origin = 'ob-archive:' + namespace
    catalog = {key: value for key, value in envelope.items() if key != 'backup_b64'}
    store.save_import_record(origin, 'backup.zip', base64.b64decode(envelope['backup_b64'], validate=True))
    store.save_import_record(origin, 'catalog.json', encode(catalog).encode())
    # Runtime SQLite triggers queue relationship generation and paid tagging on
    # ordinary Scene writes. A historical preservation import must not invoke
    # those pipelines. Remove only rows for objects created by this transaction;
    # existing/reused objects and their queued work remain untouched. Embedding
    # indexing is also deferred: all new objects stay out of automatic recall.
    suppressed = {'scene_jobs': 0, 'tagging_outbox': 0, 'index_outbox': 0}
    has_tagging = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tagging_outbox'").fetchone()
    for item in created:
        if item['kind'] == 'plan':
            continue
        suppressed['scene_jobs'] += store.conn.execute('DELETE FROM scene_jobs WHERE scene_id=?', (item['id'],)).rowcount
        suppressed['index_outbox'] += store.conn.execute('DELETE FROM index_outbox WHERE document_id=?', (item['id'],)).rowcount
        if has_tagging:
            suppressed['tagging_outbox'] += store.conn.execute('DELETE FROM tagging_outbox WHERE document_id=?', (item['id'],)).rowcount
    return {'sources': source_items, 'typed': typed_items, 'created': created,
            'counts': {'sources': dict(Counter(item['action'] for item in source_items)),
                       'typed': dict(Counter(item['action'] for item in typed_items)),
                       'created_kinds': dict(Counter(item['kind'] for item in created)),
                       'archived_originals': len(envelope['records']),
                       'erased_originals': sum(row['genre'] == 'erased' for row in envelope['records'])},
            'identity_map': mapping, 'auto_surface_enabled': 0, 'events_created': 0,
            'suppressed_background_jobs': suppressed}


def _receipt_path(namespace):
    return 'migration-' + digest(namespace) + '.json'


def preview(database, envelope):
    ob_backup.validate_envelope(envelope)
    ob_archive.preview(database, envelope)
    with tempfile.TemporaryDirectory(prefix='serein-ob-preview-') as directory:
        candidate = Path(directory)/'preview.db'
        with Store(database, read_only=True) as original, closing(sqlite3.connect(candidate)) as target:
            original.conn.backup(target)
        with Store(candidate) as store, store.transaction(immediate=True):
            result = _execute(store, envelope)
    result.update(format='ob-backup-migration-preview-v1', namespace=envelope['namespace'],
                  envelope_sha256=envelope['envelope_sha256'])
    result['preview_sha256'] = digest(encode(result))
    return result


def apply(database, envelope, *, expected_preview_sha256, backup_path):
    ob_backup.validate_envelope(envelope)
    namespace = envelope['namespace']
    origin, receipt_path = 'ob-archive:' + namespace, _receipt_path(namespace)
    with Store(database, read_only=True) as store:
        if store.conn.execute('SELECT 1 FROM import_records WHERE origin=? AND path=?', (origin, 'undo:' + receipt_path)).fetchone():
            raise Conflict('Migration was undone; use a new reviewed namespace to import again')
        old = store.conn.execute('SELECT content FROM import_records WHERE origin=? AND path=?', (origin, receipt_path)).fetchone()
        if old:
            receipt = json.loads(old[0])
            if receipt['envelope_sha256'] != envelope['envelope_sha256'] or receipt['preview_sha256'] != expected_preview_sha256:
                raise Conflict('Migration identity already used with different reviewed input')
            return receipt
    reviewed = preview(database, envelope)
    if reviewed['preview_sha256'] != expected_preview_sha256:
        raise Conflict('Migration preview changed; review the current target again')
    with Store(database) as store:
        backup = Path(backup_path)
        with backup.open('xb'):
            pass
        with closing(sqlite3.connect(backup)) as destination:
            store.conn.backup(destination)
        with store.transaction(immediate=True):
            result = _execute(store, envelope)
            comparison = {**result, 'format': reviewed['format'], 'namespace': namespace,
                          'envelope_sha256': envelope['envelope_sha256']}
            if digest(encode(comparison)) != expected_preview_sha256:
                raise Conflict('Target changed while applying; all writes rolled back')
            saved_hashes = {item['id']: digest(encode(read_plan(store, item['id']) if item['kind'] == 'plan'
                                                   else store.read(item['id']))) for item in result['created']}
            receipt = {**reviewed, 'status': 'applied', 'at': now(), 'backup_path': str(backup.resolve()),
                       'saved_hashes': saved_hashes}
            store.save_import_record(origin, receipt_path, encode(receipt).encode())
            return receipt


def undo(database, namespace):
    """Remove only this migration's unchanged new objects; retain all originals."""
    from .ob_self import assert_unreferenced
    origin, path = 'ob-archive:' + namespace, _receipt_path(namespace)
    with Store(database) as store, store.transaction(immediate=True):
        previous = store.conn.execute('SELECT content FROM import_records WHERE origin=? AND path=?',
                                      (origin, 'undo:' + path)).fetchone()
        if previous:
            return json.loads(previous[0])
        row = store.conn.execute('SELECT content FROM import_records WHERE origin=? AND path=?', (origin, path)).fetchone()
        if not row:
            raise ValueError('Migration receipt not found')
        receipt = json.loads(row[0])
        owned = {item['id'] for item in receipt['created']}
        for other in store.conn.execute('SELECT receipt_json FROM ob_type_imports'):
            later = json.loads(other[0])
            if later['status'] == 'applied' and any(item.get('id') in owned for item in later['preview']['items']):
                raise Conflict('Another import receipt depends on this migration')
        for other in store.conn.execute("SELECT origin,path,content FROM import_records WHERE origin LIKE 'ob-archive:%' "
                                        "AND origin!=? AND path LIKE 'migration-%.json'", (origin,)):
            if store.conn.execute('SELECT 1 FROM import_records WHERE origin=? AND path=?',
                                  (other['origin'], 'undo:' + other['path'])).fetchone():
                continue
            later = json.loads(other['content'])
            if any(item.get('id') in owned for item in later['sources'] + later['typed']):
                raise Conflict('Another migration receipt depends on this migration')
        for item in receipt['created']:
            key = item['id']
            value = read_plan(store, key) if item['kind'] == 'plan' else store.read(key)
            if not value or digest(encode(value)) != receipt['saved_hashes'][key]:
                raise Conflict('Imported object changed; guarded undo refuses to discard later work')
            if store.conn.execute('SELECT 1 FROM deletions WHERE document_id=?', (key,)).fetchone():
                raise Conflict('Imported object was deleted; guarded undo requires review')
            if item['kind'] != 'plan':
                has_tagging = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tagging_outbox'").fetchone()
                if store.conn.execute('SELECT 1 FROM scene_jobs WHERE scene_id=?', (key,)).fetchone() or (has_tagging and
                        store.conn.execute('SELECT 1 FROM tagging_outbox WHERE document_id=?', (key,)).fetchone()) or \
                        store.conn.execute('SELECT 1 FROM index_outbox WHERE document_id=?', (key,)).fetchone():
                    raise Conflict('Imported object has later queued processing; guarded undo requires review')
        try:
            for item in receipt['created']:
                key = item['id']
                if item['kind'] == 'plan':
                    store.conn.execute('DELETE FROM authored_plan_revisions WHERE plan_id=?', (key,))
                    store.conn.execute('DELETE FROM authored_plans WHERE id=?', (key,))
                else:
                    store.conn.execute('DELETE FROM index_outbox WHERE document_id=?', (key,))
                    store.conn.execute('DELETE FROM revisions WHERE document_id=?', (key,))
                    store.conn.execute('DELETE FROM documents WHERE id=?', (key,))
            # Own batch references disappeared together. Any remaining author,
            # narrative, shadow, plan, annotation or saved selection blocks undo.
            for item in receipt['created']:
                assert_unreferenced(store, item['id'])
        except sqlite3.IntegrityError as error:
            raise Conflict('Imported object has retained dependent rows; guarded undo refused') from error
        result = {'status': 'undone', 'namespace': namespace, 'created_removed': receipt['created'],
                  'original_archive_retained': True, 'reused_sources_changed': 0, 'at': now()}
        store.save_import_record(origin, 'undo:' + path, encode(result).encode())
        return result
