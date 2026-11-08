"""Reviewed OB feel/annotation, plan and self-content import; no network access.

Input is an explicit export plus an exact old-ID -> source-snapshot map.
Unknown fields and unresolved records remain in the retained original envelope.
"""
from pathlib import Path
import json
import sqlite3

from ..core.store import Store, Conflict, digest, encode, now
from ..core.feelings import Feelings, freeze_triggers, timestamp
from ..core.plans import Plans, read_plan
from . import ob_self


def records_from_buckets(buckets):
    """Adapt an explicit export list, without scanning a live OB directory.

    Embedded feel comments get independent parent/comment identities. Original
    feel + migrated ring pairs require review; they are never silently merged.
    """
    records = []
    for bucket in buckets:
        meta = bucket.get('metadata') or {}
        if meta.get('type', bucket.get('type')) in ('feel', 'plan') or ob_self.is_self_record(bucket):
            records.append(bucket)
        for index, comment in enumerate(meta.get('comments', [])):
            if not isinstance(comment, dict) or comment.get('kind') != 'feel':
                continue
            old_id = str(comment.get('id') or digest(encode([index, comment])))
            records.append({**comment, 'id': 'comment:' + str(bucket['id']) + ':' + old_id,
                            'type': 'annotation', 'kind': 'feel', 'parent_id': bucket['id'],
                            'legacy_comment': comment})
    return records


def _spec(store, raw, identity_map, namespace, self_mappings=None):
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not raw['id'].strip():
        raise ValueError('Every legacy record requires a stable ID')
    meta = raw.get('metadata') or {}
    if not isinstance(meta, dict):
        raise ValueError('Legacy metadata must be an object')
    values = {**meta, **raw}
    if any(level.get('deleted_at') or level.get('deleted') or level.get('locked')
           or level.get('unlock_at') or level.get('visibility') in ('locked', 'deleted')
           or level.get('lifecycle') in ('deleted', 'superseded') for level in (meta, raw)):
        return {'old_id': raw['id'], 'action': 'review', 'reason': 'restricted_or_deleted_legacy_record', 'original': raw}
    if ob_self.is_self_record(raw):
        return ob_self.spec(store, raw, namespace, (self_mappings or {}).get(raw['id']))
    kind = values.get('type')
    if kind == 'annotation' and values.get('kind') == 'feel':
        kind = 'feel'
    tags = values.get('tags') or []
    if (kind not in ('feel', 'plan') or values.get('domain') == 'whisper'
            or any(tag in tags for tag in ('whisper', '__whisper__', '__letter__', 'daily_impression', 'weekly_impression', 'relationship_weather'))):
        return {'old_id': raw['id'], 'action': 'review', 'reason': 'unsupported_genre', 'original': raw}
    key = ('scene_feel_' if kind == 'feel' else 'plan_') + digest(encode([namespace, kind, raw['id']]))
    sha = digest(encode(raw))
    compatibility = {'source_system': 'ombre_brain', 'namespace': namespace, 'old_id': raw['id'],
                     'old_type': values.get('type'), 'original': raw, 'export_sha256': sha,
                     'content_sha256': digest(values.get('content', '')),
                     'source_version_provenance': 'explicit_import_mapping; original_authored_version_not_inferred'}
    current = store.read(key) if kind == 'feel' else read_plan(store, key)
    if current:
        saved = current['metadata'].get('compatibility', {}) if kind == 'feel' else current.get('compatibility', {})
        if saved.get('export_sha256') != sha:
            raise Conflict('Legacy identity already imported with different content')
        if kind == 'feel' and current['lifecycle'] == 'deleted':
            raise Conflict('Imported feel was removed; do not resurrect it')
        return {'old_id': raw['id'], 'id': key, 'kind': kind, 'action': 'reuse'}
    content = values.get('content')
    if not isinstance(content, str) or not content.strip():
        return {'old_id': raw['id'], 'action': 'review', 'reason': 'missing_content', 'original': raw}
    old_links = values.get('source_ids')
    if old_links is None:
        old_links = [values.get('source_bucket') or values.get('triggered_by') or values.get('parent_id')] if kind == 'feel' else [values.get('related_bucket')]
    if not isinstance(old_links, list):
        raise ValueError('source_ids must be a list of exact old identities')
    old_links = [key for key in old_links if key]
    missing = [key for key in old_links if key not in identity_map]
    if missing or kind == 'feel' and not old_links:
        return {'old_id': raw['id'], 'action': 'pending_link', 'reason': 'migration_source_link_missing',
                'missing_ids': missing, 'original': raw}
    refs = [identity_map[key] for key in old_links]
    if any(not isinstance(ref, dict) or not ref.get('revision') or not ref.get('sha256') for ref in refs):
        raise ValueError('Import identity maps require reviewed source revision and hash')
    try:
        refs = freeze_triggers(store, refs) if refs else []
    except (ValueError, Conflict) as exc:
        return {'old_id': raw['id'], 'action': 'pending_link', 'reason': str(exc), 'original': raw}
    authored = (values.get('authored_at') or values.get('original_feel_created')
                or values.get('created_at') or values.get('createdAt') or values.get('created'))
    if authored:
        try:
            timestamp(authored)
        except (ValueError, TypeError):
            return {'old_id': raw['id'], 'action': 'review', 'reason': 'unresolved_authored_time_timezone', 'original': raw}
    compatibility['authored_at'] = authored  # None remains unknown, never an export date.
    options = {'authored_at': authored, 'legacy': compatibility}
    if kind == 'feel':
        flags = {}
        for flag in ('digested', 'dont_surface', 'pinned', 'protected'):
            if flag in values:
                if type(values[flag]) not in (bool, int) or values[flag] not in (False, True):
                    return {'old_id': raw['id'], 'action': 'review', 'reason': 'ambiguous_legacy_flags', 'original': raw}
                flags[flag] = bool(values[flag])
        manual = values.get('manual_surface')
        if manual not in (None, 0, 1, False, True):
            return {'old_id': raw['id'], 'action': 'review', 'reason': 'ambiguous_surface_flag', 'original': raw}
        options.update(author=values.get('author') or 'legacy_author_unknown', title=values.get('title', ''),
                       valence=values.get('valence'), arousal=values.get('arousal'),
                       can_surface=bool(manual) if manual is not None else None, flags=flags,
                       lifecycle='archived' if values.get('archived') or values.get('lifecycle') == 'archived' else 'active',
                       document_id=key)
    else:
        status = values.get('status', 'active')
        if status not in ('active', 'resolved', 'abandoned'):
            return {'old_id': raw['id'], 'action': 'review', 'reason': 'unrecognized_plan_state', 'original': raw}
        options.update(actor=values.get('author') or 'legacy_actor_unknown', weight=values.get('weight', 0.5),
                       status=status, plan_id=key)
    return {'old_id': raw['id'], 'id': key, 'kind': kind, 'action': 'create',
            'content': content, 'references': refs, 'options': options,
            'source_preconditions': {ref['id']: digest(encode(store.read(ref['id'])))
                                     for ref in refs if ref['kind'] in ('scene', 'event')}}


def preview_in_store(store, records, identity_map, namespace, self_mappings=None):
    if not isinstance(records, list) or not 1 <= len(records) <= 200 or not namespace.strip():
        raise ValueError('Use an explicit namespace and 1..200 records')
    if len({r.get('id') for r in records}) != len(records):
        raise ValueError('Duplicate export identities require review')
    if self_mappings is not None and (not isinstance(self_mappings, dict)
            or set(self_mappings) - {row['id'] for row in records if ob_self.is_self_record(row)}):
        raise ValueError('self_mappings must name exact self record IDs in this batch')
    items = [_spec(store, row, identity_map, namespace, self_mappings) for row in records]
    result = {'schema': 'ob-types-v1', 'namespace': namespace, 'items': items,
              'counts': {action: sum(item['action'] == action for item in items)
                         for action in ('create', 'reuse', 'pending_link', 'review')},
              'reminders_created': 0, 'production_behavior_verified': False}
    result['potential_duplicates'] = []
    original_ids = {raw['id'] for raw in records}
    for raw in records:
        original_feel = raw.get('original_feel_id') or (raw.get('metadata') or {}).get('original_feel_id')
        if original_feel and original_feel in original_ids:
            result['potential_duplicates'].append({'ids': [original_feel, raw['id']],
                                                    'reason': 'explicit_original_feel_identity; review_before_import'})
    if result['potential_duplicates']:
        duplicate_ids = {key for pair in result['potential_duplicates'] for key in pair['ids']}
        for item in items:
            if item['old_id'] in duplicate_ids and item['action'] == 'create':
                item['action'] = 'review'
                item['reason'] = 'legacy_feel_and_migrated_ring_both_present'
                result['counts']['create'] -= 1
                result['counts']['review'] += 1
    result['preview_sha256'] = digest(encode(result))
    return result


def preview(database, records, identity_map, *, namespace, self_mappings=None):
    with Store(database, read_only=True) as store:
        return preview_in_store(store, records, identity_map, namespace, self_mappings)


def apply(database, records, identity_map, *, namespace, batch_id, expected_preview_sha256, backup_path,
          self_mappings=None):
    if not batch_id.strip():
        raise ValueError('A stable batch ID is required')
    request = [records, identity_map, namespace, expected_preview_sha256]
    if self_mappings:
        request.append(self_mappings)  # Preserve old feel/plan retry identities.
    request_sha = digest(encode(request))
    with Store(database, read_only=True) as existing:
        if existing.conn.execute('PRAGMA user_version').fetchone()[0] != 10:
            raise ValueError('Back up and explicitly initialize schema v10 before applying a type import')
    with Store(database) as store:
        # The local full SQLite backup is exclusive and precedes every write.
        old = store.conn.execute('SELECT * FROM ob_type_imports WHERE batch_id=?', (batch_id,)).fetchone()
        if old:
            if old['request_sha256'] != request_sha:
                raise Conflict('Batch ID already used with different arguments')
            return json.loads(old['receipt_json'])
        backup = Path(backup_path)
        with backup.open('xb'):
            pass
        with sqlite3.connect(backup) as target:
            store.conn.backup(target)
        with store.transaction(immediate=True):
            concurrent = store.conn.execute('SELECT * FROM ob_type_imports WHERE batch_id=?', (batch_id,)).fetchone()
            if concurrent:
                if concurrent['request_sha256'] != request_sha:
                    raise Conflict('Batch ID already used with different arguments')
                return json.loads(concurrent['receipt_json'])
            reviewed = preview_in_store(store, records, identity_map, namespace, self_mappings)
            if reviewed['preview_sha256'] != expected_preview_sha256:
                raise Conflict('Preview changed; review the current sources and targets again')
            created = []
            for raw in records:
                store.save_import_record('ob-types:' + namespace, raw['id'], encode(raw).encode('utf-8'))
            for item in reviewed['items']:
                if item['action'] != 'create':
                    continue
                if item['kind'] in ('scene', 'shadow'):
                    saved = ob_self.create_in_store(store, item)
                    stamp = digest(encode(saved))
                elif item['kind'] == 'feel':
                    saved = Feelings.create_in_store(store, item['content'], item['references'], **item['options'])
                    stamp = digest(encode(saved))
                else:
                    saved = Plans.create_in_store(store, item['content'], related=item['references'], **item['options'])
                    stamp = digest(encode(saved))
                created.append({'id': item['id'], 'kind': item['kind'], 'saved_sha256': stamp})
            receipt = {'batch_id': batch_id, 'status': 'applied', 'created': created,
                       'preview': reviewed, 'backup_path': str(backup.resolve()), 'at': now()}
            store.conn.execute('INSERT INTO ob_type_imports VALUES (?,?,?)', (batch_id, request_sha, encode(receipt)))
            return receipt


def undo(database, batch_id):
    """Remove only unchanged, unreferenced creations; never restore the full DB."""
    with Store(database) as store, store.transaction(immediate=True):
        row = store.conn.execute('SELECT receipt_json FROM ob_type_imports WHERE batch_id=?', (batch_id,)).fetchone()
        if not row:
            raise ValueError('Import receipt not found')
        receipt = json.loads(row[0])
        if receipt['status'] == 'undone':
            return receipt
        for item in receipt['created']:
            saved = (ob_self.read_saved(store, item['id'], item['kind']) if item['kind'] in ('scene', 'shadow')
                     else store.read(item['id']) if item['kind'] == 'feel' else read_plan(store, item['id']))
            if not saved or digest(encode(saved)) != item['saved_sha256']:
                raise Conflict('Imported object was edited; targeted undo requires review')
            if store.conn.execute('SELECT 1 FROM deletions WHERE document_id=?', (item['id'],)).fetchone():
                raise Conflict('Imported object was deleted; targeted undo requires review')
            if item['kind'] in ('scene', 'shadow'):
                ob_self.assert_unreferenced(store, item['id'])
            for other in store.conn.execute('SELECT receipt_json FROM ob_type_imports WHERE batch_id!=?', (batch_id,)):
                later = json.loads(other[0])
                if later['status'] == 'applied' and any(
                        record.get('id') == item['id'] for record in later['preview']['items']):
                    raise Conflict('Another import receipt depends on this object')
            if item['kind'] in ('feel', 'scene'):
                from ..core.feelings import backlinks as feels
                from ..core.plans import backlinks as plans
                if feels(store, item['id']) or plans(store, item['id']) or store.conn.execute(
                    'SELECT 1 FROM narrative_materials WHERE target_id=?', (item['id'],)).fetchone():
                    raise Conflict('Imported object has new references')
        for item in receipt['created']:
            key = item['id']
            if item['kind'] in ('feel', 'scene'):
                store.conn.execute('DELETE FROM index_outbox WHERE document_id=?', (key,))
                store.conn.execute('DELETE FROM revisions WHERE document_id=?', (key,))
                store.conn.execute('DELETE FROM documents WHERE id=?', (key,))
            elif item['kind'] == 'shadow':
                store.conn.execute('DELETE FROM historical_works WHERE id=?', (key,))
            else:
                store.conn.execute('DELETE FROM authored_plan_revisions WHERE plan_id=?', (key,))
                store.conn.execute('DELETE FROM authored_plans WHERE id=?', (key,))
        receipt.update(status='undone', undone_at=now())
        store.conn.execute('UPDATE ob_type_imports SET receipt_json=? WHERE batch_id=?', (encode(receipt), batch_id))
        return receipt


def export_types(database):
    """Local, exact version archive; full database backup remains the recovery source."""
    with Store(database, read_only=True) as store:
        store.conn.execute('BEGIN')
        feelings = []
        for row in store.conn.execute(
            "SELECT d.id FROM documents d JOIN revisions r ON r.document_id=d.id AND r.number=d.revision "
            "WHERE json_extract(r.metadata_json,'$.memory_role')='feel' ORDER BY d.id"):
            feelings.append({'document': store.read(row[0]), 'versions': [dict(v) for v in store.conn.execute(
                'SELECT * FROM revisions WHERE document_id=? ORDER BY number', (row[0],))]})
        return {'format': 'serein-authored-types-v1', 'feelings': feelings,
                'self_content': ob_self.export_in_store(store),
                'original_records': [dict(r) | {'content': json.loads(r['content'])} for r in store.conn.execute(
                    "SELECT * FROM import_records WHERE origin LIKE 'ob-types:%' ORDER BY origin,path")],
                'plans': [read_plan(store, r[0]) for r in store.conn.execute('SELECT id FROM authored_plans ORDER BY id')],
                'plan_versions': [dict(r) for r in store.conn.execute('SELECT * FROM authored_plan_revisions ORDER BY plan_id,revision')],
                'import_receipts': [dict(r) for r in store.conn.execute('SELECT * FROM ob_type_imports ORDER BY batch_id')]}
