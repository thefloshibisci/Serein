"""Narrative registry and review queue persisted in the canonical SQLite database."""

import json
from copy import deepcopy
from contextlib import contextmanager
from threading import RLock

from ..core.store import Store, digest, encode, now
from ..core.notebook import resolve_entry
from ..ingest.legacy_narrative import KINDS, add_material
from .germany.narrative_rolls import NarrativeRollStore, _extract_body
from .germany.narrative_revision_inbox import NarrativeRevisionInbox
from .germany.narrative_uploads import (
    NarrativeUploadStore, _safe_filename, extract_upload_text, MAX_UPLOAD_BYTES,
)


@contextmanager
def narrative_transaction(database, *, write=False):
    with Store(database, read_only=not write) as store:
        store.conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield Narratives(store)
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise


class Narratives(NarrativeRollStore):
    def __init__(self, store):
        self.store = store
        from ..deployment import read_from_store, expanded_identity
        self.identity = expanded_identity(read_from_store(store)['identity'])
        self.live_injection_enabled = False

    def _registry(self):
        return {'schema_version': 'narrative-roll-registry-v1', 'rolls': [
            self.store.read(row[0])['metadata']['legacy_registry']
            for row in self.store.conn.execute("SELECT id FROM documents WHERE kind='narrative' AND lifecycle!='deleted'")
        ]}

    def _load(self):
        items = []
        for entry in self._registry()['rolls']:
            doc = self.store.read(entry['narrative_id'])
            document = doc['body_md']
            body = _extract_body(document)
            stamp = digest(document)
            integrity = ('hash_mismatch' if entry.get('document_sha256') and entry['document_sha256'] != stamp
                         else 'missing_first_person_body' if not body and entry.get('publication_status') != 'collecting'
                         else 'ok')
            item = {**entry, 'integrity_status': integrity, 'actual_document_sha256': stamp,
                    'body': body if integrity == 'ok' else '', 'body_sha256': digest(body) if body else '',
                    'body_chars': len(body), 'full_document': document if integrity == 'ok' else ''}
            for kind in KINDS:
                excluded = set(entry.get(f'excluded_{kind}_ids') or [])
                linked = getattr(self, f'source_{kind}_ids')(document, entry.get(f'linked_{kind}_ids'))
                linked = [key for key in linked if key not in excluded]
                item[f'linked_{kind}_ids'] = linked
                item[f'linked_{kind}_count'] = len(linked)
            items.append(item)
        return items

    def body_material_ids(self, narrative):
        """Recover the source baseline before material-only revisions of this body."""
        baseline = {f'{kind}_ids': [] for kind in KINDS}
        body = str(narrative.get('body') or '').strip()
        if not body:
            return baseline
        rows = self.store.conn.execute(
            'SELECT body_md,metadata_json FROM revisions WHERE document_id=? '
            'AND number<=? ORDER BY number DESC',
            (narrative['narrative_id'], narrative['revision']))
        for row in rows:
            if _extract_body(row['body_md']) != body:
                break
            entry = json.loads(row['metadata_json']).get('legacy_registry', {})
            for kind in KINDS:
                excluded = set(entry.get(f'excluded_{kind}_ids') or [])
                linked = getattr(self, f'source_{kind}_ids')(
                    row['body_md'], entry.get(f'linked_{kind}_ids'))
                baseline[f'{kind}_ids'] = [key for key in linked if key not in excluded]
        return baseline

    def _persist(self, entry, document):
        # Caller holds BEGIN IMMEDIATE from the first CAS/source read to commit.
        key = entry['narrative_id']
        current = self.store.read(key)
        metadata = {'legacy_registry': entry, 'body_format': 'legacy_full_document'}
        if current:
            self.store.revise(key, expected_revision=entry['revision'] - 1,
                              title=entry['title'], body_md=document, metadata=metadata)
        else:
            self.store.create(key, 'narrative', entry['title'], document, metadata=metadata,
                              manual_surface=None, created_at=entry['published_at'])
        self.store.set_lifecycle(key, 'active' if entry['lifecycle'] == 'active' else 'archived')
        for kind in KINDS:
            for disposition in ('linked', 'excluded'):
                field = f'{disposition}_{kind}_ids'
                for target in entry.get(field) or []:
                    add_material(self.store, key, entry['revision'], 'registry:' + field,
                                 kind, target, disposition, {'source_file': entry['source_file']})
        for row in self.store.conn.execute('SELECT * FROM event_arc_links WHERE arc_key=?', (entry.get('arc_key') or '',)):
            add_material(self.store, key, entry['revision'], 'arc_event_links', 'event',
                         row['event_id'], 'appended', json.loads(row['metadata_json']))
        self.store.conn.execute('INSERT INTO index_outbox(document_id) VALUES (?)', (key,))

    def append_materials_without_body(self, narrative_id, additions, *, model=''):
        """Append verified material membership while preserving authored prose and publish time."""
        current = self.read(narrative_id)
        if current.get('status') != 'ok' or current.get('lifecycle') != 'active':
            return {'status': 'conflict', 'reason': 'narrative_unavailable', 'narrative_id': narrative_id}
        entry = deepcopy(self.store.read(narrative_id)['metadata']['legacy_registry'])
        old_entry = deepcopy(entry)
        added = {kind: [] for kind in ('event', 'scene', 'diary')}
        for kind in added:
            field = f'linked_{kind}_ids'
            existing = list(entry.get(field) or [])
            excluded = {str(value) for value in entry.get(f'excluded_{kind}_ids') or []}
            for raw in additions.get(f'{kind}_ids') or []:
                key = int(raw) if kind == 'diary' else str(raw)
                document = self.store.read(str(key)) if kind != 'diary' else None
                available = (resolve_entry(self.store, key, kind='diary')['resolution'] == 'active'
                             if kind == 'diary' else bool(document and document['kind'] == kind and document['lifecycle'] == 'active'))
                if not available or str(key) in excluded or key in existing:
                    continue
                existing.append(key)
                added[kind].append(key)
            entry[field] = existing
        if not any(added.values()):
            return {'status': 'idempotent', 'narrative_id': narrative_id, 'added': added}
        previous = old_entry
        previous.pop('history', None)
        entry['history'] = [*entry.get('history', []), previous]
        entry['revision'] = int(entry.get('revision') or 0) + 1
        entry['source_file'] = f"sqlite:{narrative_id}/revision-{entry['revision']:04d}"
        entry['published_by'] = 'serein_auto_arc_scout'
        ledger = [f'- {kind}:{key}' for kind, ids in added.items() for key in ids]
        document = current['full_document'].rstrip() + '\n\n## 自动追加材料\n\n' + '\n'.join(ledger) + '\n'
        entry['document_sha256'] = digest(document)
        self._persist(entry, document)
        return {'status': 'updated', 'narrative_id': narrative_id, 'revision': entry['revision'],
                'added': added, 'model': str(model or ''), 'body_unchanged': True}


class RevisionInbox(NarrativeRevisionInbox):
    def __init__(self, store):
        self.store = store
        self._lock = RLock()

    def list(self, **kwargs):
        """Keep program-generated Arc hints visible, including older cue matches."""
        return super().list(exclude_proposal_kind='new_roll_candidate', **kwargs)

    def retire_model_candidates(self):
        raw = self._load()
        retired = []
        for item in raw['items']:
            if item.get('proposal_kind') == 'new_roll_candidate' and item.get('status') == 'pending':
                item.update(status='dismissed', resolution='automatic_candidate_retired',
                            reviewed_at=now(), updated_at=now())
                retired.append(item['proposal_id'])
        if retired:
            self._save(raw)
        return retired

    def consider_new_roll_candidates(self, candidates, *, model):
        # Scout ran outside the write transaction. Recheck current source access
        # and membership before accepting its derived grouping or additions.
        bound = set()
        for roll in Narratives(self.store)._load():
            for kind in ('event', 'scene'):
                bound.update((kind, str(key)) for key in roll.get(f'linked_{kind}_ids') or [])
            if roll.get('arc_key'):
                bound.update(('event', row[0]) for row in self.store.conn.execute(
                    'SELECT event_id FROM fact_event_arc_links WHERE arc_key=?', (roll['arc_key'],)))
        valid = []
        for candidate in candidates:
            refs = [(kind, str(key)) for kind in ('event', 'scene') for key in candidate.get(f'source_{kind}_ids') or []]
            if all((kind, key) not in bound and (kind != 'event' or not self.store.promoted_scene(key))
                   and (doc := self.store.read(key)) and
                   doc['kind'] == kind and doc['lifecycle'] == 'active' for kind, key in refs):
                valid.append(candidate)
        return super().consider_new_roll_candidates(valid, model=model)

    def mark_absorbed(self, narrative_id, *, source_scene_ids, revision):
        changed = super().mark_absorbed(narrative_id, source_scene_ids=source_scene_ids, revision=revision)
        raw = self._load()
        resolved = [item for item in raw['items'] if item.get('resolved_narrative_id') == narrative_id]
        for item in resolved:
            item.update(resolution='written', absorbed_revision=revision, updated_at=now())
            changed.append(item['proposal_id'])
        if resolved:
            self._save(raw)
        return changed

    def _load(self):
        rows = self.store.conn.execute('SELECT metadata_json FROM narrative_proposals ORDER BY rowid')
        state = self.store.conn.execute("SELECT value_json FROM background_state WHERE name='narrative_inbox'").fetchone()
        return {**(json.loads(state[0]) if state else {}),
                'schema_version': 'narrative-revision-inbox-v1', 'items': [json.loads(row[0]) for row in rows]}

    def _save(self, raw):
        # Review/scout operations run inside the same database transaction as a
        # body save. Deletions only remove proposals explicitly reconciled away.
        items = raw['items']
        self.store.conn.execute("INSERT INTO background_state VALUES ('narrative_inbox',?) "
            "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",
            (encode({key:value for key,value in raw.items() if key!='items'}),))
        keep = {item['proposal_id'] for item in items}
        for row in self.store.conn.execute('SELECT id FROM narrative_proposals').fetchall():
            if row[0] not in keep:
                self.store.conn.execute('DELETE FROM narrative_proposals WHERE id=?', (row[0],))
        for item in items:
            self.store.conn.execute('INSERT INTO narrative_proposals VALUES (?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET narrative_id=excluded.narrative_id,status=excluded.status,metadata_json=excluded.metadata_json',
                (item['proposal_id'], 'serein-live', item.get('narrative_id') or '', item['status'], encode(item)))


class Uploads:
    def __init__(self, store):
        self.store = store

    def read(self, upload_id, *, include_text=True):
        row = self.store.conn.execute('SELECT u.metadata_json,r.content FROM narrative_uploads u '
            'JOIN import_records r ON r.origin=u.origin AND r.path=u.path WHERE u.id=?', (upload_id,)).fetchone()
        if not row:
            return {'status': 'not_found', 'upload_id': upload_id}
        record = json.loads(row[0])
        if len(row[1]) != record['size'] or digest(row[1]) != record['sha256']:
            return {'status': 'invalid', 'reason': 'upload_blob_mismatch', 'upload_id': upload_id}
        result = {'status': 'ok', **NarrativeUploadStore._public(record)}
        if include_text:
            result['extracted_text'] = record.get('extracted_text') or ''
        return result

    def create(self, raw, *, filename, content_type=''):
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            return {'status': 'invalid', 'reason': 'empty_upload' if not raw else 'upload_too_large', 'writes_performed': []}
        stamp = digest(raw)
        key = 'upload_' + stamp[:32]
        existing = self.read(key, include_text=False)
        if existing['status'] != 'not_found':
            return {**existing, 'created': False, 'writes_performed': []}
        filename = _safe_filename(filename)
        mime = str(content_type or 'application/octet-stream').split(';', 1)[0].strip().lower()[:160]
        content, status = extract_upload_text(raw, filename, mime)
        record = {'upload_id': key, 'filename': filename, 'content_type': mime, 'size': len(raw),
                  'sha256': stamp, 'extraction_status': status, 'extracted_text': content,
                  'created_at': now(), 'blob_name': stamp + '.bin'}
        path = 'uploads/blobs/' + record['blob_name']
        self.store.save_import_record('serein-live', path, raw)
        self.store.conn.execute('INSERT INTO narrative_uploads VALUES (?,?,?,?)',
                                (key, 'serein-live', path, encode(record)))
        return {'status': 'ok', 'created': True, **NarrativeUploadStore._public(record), 'writes_performed': [key]}


def seed_narrative_runtime(database):
    """Restore the captured scan checkpoint once; never reset a live scheduler."""
    with Store(database) as store,store.transaction():
        if store.conn.execute("SELECT 1 FROM background_state WHERE name='narrative_inbox'").fetchone():return
        rows=store.conn.execute("SELECT content FROM import_records WHERE path='revision_inbox.json'").fetchall()
        if len(rows)!=1:raise ValueError('Expected one captured Narrative inbox')
        inbox=json.loads(rows[0][0])
        store.conn.execute('INSERT INTO background_state VALUES (?,?)',('narrative_inbox',encode({key:value for key,value in inbox.items() if key!='items'})))
