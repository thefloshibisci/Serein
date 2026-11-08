"""Explicit mapping of old self prose into existing Scenes or historical shadows.

No fixed self category, portrait generation, or automatic continuity adoption.
"""
import json

from ..core.feelings import freeze_triggers, timestamp
from ..core.store import Conflict, digest, encode, now
from ..legacy_migration.scan import is_old_self_anchor


def is_self_record(raw):
    meta = raw.get('metadata') or {}
    return isinstance(meta, dict) and (is_old_self_anchor(meta) or is_old_self_anchor(raw)
        or any(level.get('self_anchor') not in (None, False, 0, '') for level in (meta, raw)))


def read_saved(store, key, kind):
    if kind == 'scene':
        return store.read(key)
    row = store.conn.execute('SELECT * FROM historical_works WHERE id=?', (key,)).fetchone()
    return dict(row) if row else None


def spec(store, raw, namespace, decision):
    values = {**(raw.get('metadata') or {}), **raw}
    def review(reason):
        return {'old_id': raw['id'], 'action': 'review', 'reason': reason, 'original': raw}
    if values.get('type') in ('feel', 'plan', 'whisper', 'annotation'):
        return review('mixed_self_genre_requires_review')
    if values.get('lifecycle') not in (None, 'active', 'archived'):
        return review('unrecognized_self_lifecycle')
    if decision is None:
        return review('self_mapping_required: scene or historical_shadow')
    if not isinstance(decision, dict) or set(decision) - {'target', 'scene_refs'}:
        raise ValueError('Self mapping accepts only target and reviewed scene_refs')
    target = decision.get('target')
    if target not in ('scene', 'historical_shadow'):
        raise ValueError('Self target must be scene or historical_shadow')
    content = values.get('content')
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        return review('self_content_requires_1_to_100000_characters')
    authored = values.get('authored_at') or values.get('created_at') or values.get('createdAt') or values.get('created')
    if authored:
        try:
            timestamp(authored)
        except (ValueError, TypeError):
            return review('unresolved_authored_time_timezone')
    flags = {}
    for name in ('digested', 'dont_surface', 'pinned', 'protected', 'archived', 'manual_surface', 'self_anchor'):
        if name in values and values[name] is not None:
            if type(values[name]) not in (bool, int) or values[name] not in (0, 1):
                return review('ambiguous_legacy_flags')
            flags[name] = bool(values[name])
    refs = decision.get('scene_refs', [])
    if not isinstance(refs, list) or (target == 'scene' and refs):
        raise ValueError('scene_refs are optional reviewed links for historical_shadow only')
    if any(not isinstance(ref, dict) or ref.get('kind') != 'scene'
           or type(ref.get('revision')) is not int or not ref.get('sha256') for ref in refs):
        raise ValueError('Shadow Scene links require exact kind, ID, revision and hash')
    if refs:
        try:
            refs = freeze_triggers(store, refs)
        except (ValueError, Conflict) as exc:
            return review('self_scene_link_unavailable: ' + str(exc))
    identity = digest(encode([namespace, 'self', raw['id']]))
    keys = {'scene': 'scene_ob_self_' + identity, 'historical_shadow': 'window_ob_self_' + identity}
    key = keys[target]
    kind = 'scene' if target == 'scene' else 'shadow'
    compatibility = {'source_system': 'ombre_brain', 'namespace': namespace, 'old_id': raw['id'],
                     'old_type': values.get('type'), 'source_role': 'self_authored',
                     'original': raw, 'export_sha256': digest(encode(raw)),
                     'content_sha256': digest(content), 'authored_at': authored or None,
                     'mapping': decision, 'mapping_sha256': digest(encode(decision)),
                     'provenance': 'legacy_authored_self; not verified chat evidence'}
    for choice, candidate in keys.items():
        if store.conn.execute('SELECT 1 FROM deletions WHERE document_id=?', (candidate,)).fetchone():
            raise Conflict('Imported self content was deleted; do not resurrect it')
        current = read_saved(store, candidate, 'scene' if choice == 'scene' else 'shadow')
        if not current:
            continue
        meta = current['metadata'] if choice == 'scene' else json.loads(current['metadata_json'])
        old = meta.get('compatibility', {})
        if (candidate != key or old.get('export_sha256') != compatibility['export_sha256']
                or old.get('mapping_sha256') != compatibility['mapping_sha256']):
            raise Conflict('Self identity already imported with different content or mapping')
        if current.get('lifecycle') == 'deleted':
            raise Conflict('Imported self content was deleted; do not resurrect it')
        return {'old_id': raw['id'], 'id': key, 'kind': kind, 'action': 'reuse',
                'saved_sha256': digest(encode(current))}
    return {'old_id': raw['id'], 'id': key, 'kind': kind, 'action': 'create',
            'content': content, 'title': str(values.get('title') or values.get('name') or raw['id']),
            'compatibility': compatibility, 'references': refs, 'legacy_flags': flags,
            'lifecycle': 'archived' if flags.get('archived') or values.get('type') in ('archive', 'archived')
                         or values.get('lifecycle') == 'archived' else 'active',
            'automatic_surface': False, 'adopts_current_self': False,
            'source_preconditions': {ref['id']: digest(encode(store.read(ref['id']))) for ref in refs}}


def create_in_store(store, item):
    stamp = now()
    compatibility = item['compatibility']
    meta = {'compatibility': compatibility, 'authored_at': compatibility['authored_at'],
            'occurred_at': None, 'imported_at': stamp, 'legacy_flags': item['legacy_flags'],
            'memory_value_source': 'legacy_authored_self', 'source_time_basis': 'authored_at',
            'self_context_refs': item['references']}
    if item['kind'] == 'scene':
        # Old self/pinned/protected flags do not grant ordinary recall or favorites.
        saved = store.create(item['id'], 'scene', item['title'], item['content'],
            metadata=meta, manual_surface=False, lifecycle=item['lifecycle'],
            created_at=compatibility['authored_at'])
        store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', (item['id'],))
        return saved
    meta.update(window_id=item['id'], revision_root_id=item['id'], revision=1,
                title=item['title'], created_at=compatibility['authored_at'], updated_at=stamp,
                continuity_eligible=False, lifecycle=item['lifecycle'],
                sections_json=encode({'self_view': item['content']}),
                moment_bucket_ids_json=encode([ref['id'] for ref in item['references']]))
    store.conn.execute('INSERT INTO historical_works VALUES (?,\'shadow\',1,?,?,?,?,?,?)',
        (item['id'], item['title'], item['content'], digest(item['content']), encode(meta),
         'ob-types:' + compatibility['namespace'], compatibility['old_id']))
    return read_saved(store, item['id'], 'shadow')


def assert_unreferenced(store, key):
    """Refuse targeted removal when saved reading/linking decisions depend on it."""
    queries = [
        ('SELECT 1 FROM narrative_materials WHERE target_id=?', (key,)),
        ('SELECT 1 FROM scene_relations WHERE source_scene_id=? OR target_scene_id=?', (key, key)),
        ('SELECT 1 FROM scene_proposals WHERE source_scene_id=? OR target_scene_id=?', (key, key)),
        ('SELECT 1 FROM personal_records WHERE document_id=?', (key,)),
        ('SELECT 1 FROM evidence_bindings WHERE document_id=?', (key,)),
        ('SELECT 1 FROM revisions r,json_tree(r.metadata_json) j WHERE r.document_id!=? AND j.atom=?', (key, key)),
        ('SELECT 1 FROM historical_works w,json_tree(w.metadata_json) j WHERE w.id!=? AND j.atom=?', (key, key)),
        ("SELECT 1 FROM historical_works w,json_each(COALESCE(json_extract(w.metadata_json,"
         "'$.moment_bucket_ids_json'),'[]')) j WHERE w.id!=? AND j.value=?", (key, key)),
        ('SELECT 1 FROM authored_plan_revisions r,json_tree(r.payload_json) j WHERE j.atom=?', (key,)),
        ('SELECT 1 FROM background_state b,json_tree(b.value_json) j WHERE j.atom=?', (key,)),
        ('SELECT 1 FROM historical_work_events WHERE work_id=?', (key,)),
    ]
    if any(store.conn.execute(query, params).fetchone() for query, params in queries):
        raise Conflict('Imported self content has new references; review before undo')


def export_in_store(store):
    scenes = []
    for row in store.conn.execute("SELECT d.id FROM documents d JOIN revisions r "
            "ON r.document_id=d.id AND r.number=d.revision "
            "WHERE json_extract(r.metadata_json,'$.compatibility.source_role')='self_authored' ORDER BY d.id"):
        scenes.append({'document': store.read(row[0]), 'versions': [dict(v) for v in store.conn.execute(
            'SELECT * FROM revisions WHERE document_id=? ORDER BY number', (row[0],))]})
    shadows = [dict(row) for row in store.conn.execute("SELECT * FROM historical_works "
        "WHERE json_extract(metadata_json,'$.compatibility.source_role')='self_authored' ORDER BY id")]
    return {'scenes': scenes, 'shadows': shadows}
