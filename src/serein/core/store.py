"""SQLite persistence; reading and evidence coverage do not rewrite documents."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def promoted_scene_id(event_id):
    return "scene_" + digest("promoted-event:" + event_id)


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path, *, read_only=False):
        self.path = Path(path)
        self.conn = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
                     if read_only else sqlite3.connect(self.path, isolation_level=None))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if read_only:
            if version not in (5, 6, 7, 8, 9, 10) or self.conn.execute("PRAGMA application_id").fetchone()[0] != 0x53524E31:
                self.close()
                raise ValueError("Read-only access requires an initialized Serein schema v5..v10 database")
            return
        if version == 0:
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                self.close()
                raise ValueError("Refusing to initialize an existing non-Serein database")
            schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + schema + "\nCOMMIT;")
            version = 1
        elif version not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10) or self.conn.execute("PRAGMA application_id").fetchone()[0] != 0x53524E31:
            self.close()
            raise ValueError(f"Unsupported Serein schema version: {version}")
        if version == 1:
            migration = Path(__file__).with_name("schema_v2.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")
            version = 2
        if version == 2:
            migration = Path(__file__).with_name("schema_v3.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")
            version = 3
        if version == 3:
            migration = Path(__file__).with_name("schema_v4.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")
            version = 4
        if version == 4:
            migration = Path(__file__).with_name("schema_v5.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")
            version = 5
        if version == 5:
            migration = Path(__file__).with_name("schema_v6.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")
            version = 6
        if version == 6:
            migration = Path(__file__).with_name("schema_v7.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")
            version = 7
        if version == 7:
            migration = Path(__file__).with_name("schema_v8.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")
            version = 8
        if version == 8:
            migration = Path(__file__).with_name("schema_v9.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")
            version = 9
        if version == 9:
            migration = Path(__file__).with_name("schema_v10.sql").read_text(encoding="utf-8")
            self.conn.executescript("BEGIN;\n" + migration + "\nCOMMIT;")

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self, *, immediate=False):
        # Savepoints allow an importer to commit a whole batch atomically.
        owns_transaction = immediate and not self.conn.in_transaction
        if owns_transaction:
            self.conn.execute('BEGIN IMMEDIATE')
        name = "s" + uuid4().hex
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            if owns_transaction:
                self.conn.rollback()
            raise
        else:
            self.conn.execute(f"RELEASE {name}")
            if owns_transaction:
                self.conn.commit()

    def create(self, document_id, kind, title, body_md, *, lifecycle="active",
               manual_surface=True, metadata=None, revision=1, created_at=None,
               updated_at=None):
        timestamp = now()
        with self.transaction(immediate=True):
            if self.read(document_id) or self.conn.execute(
                "SELECT 1 FROM deletions WHERE document_id=?", (document_id,)
            ).fetchone():
                raise Conflict(f"Document ID already exists or was deleted: {document_id}")
            self.conn.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, kind, revision, lifecycle, manual_surface,
                 created_at or timestamp, updated_at or timestamp),
            )
            self._add_revision(document_id, revision, title, body_md,
                               metadata or {}, updated_at or timestamp)
        return self.read(document_id)

    def _add_revision(self, document_id, number, title, body_md, metadata, recorded_at):
        self.conn.execute(
            "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (document_id, number, title, body_md, digest(body_md),
             encode(metadata), recorded_at),
        )

    def read(self, document_id, *, revision=None):
        row = self.conn.execute(
            "SELECT d.*, r.number AS body_revision, r.title, r.body_md, "
            "r.body_sha256, r.metadata_json FROM documents d JOIN revisions r "
            "ON r.document_id=d.id AND r.number=COALESCE(?, d.revision) WHERE d.id=?",
            (revision, document_id),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def revise(self, document_id, *, expected_revision, title, body_md, metadata=None):
        with self.transaction(immediate=True):
            current = self.read(document_id)
            if not current or current["revision"] != expected_revision:
                raise Conflict("Document revision changed or does not exist")
            if current["lifecycle"] == "deleted":
                raise Conflict("A deleted document cannot be edited")
            number = expected_revision + 1
            timestamp = now()
            self._add_revision(document_id, number, title, body_md,
                               current["metadata"] if metadata is None else metadata, timestamp)
            self.conn.execute(
                "UPDATE documents SET revision=?,updated_at=? WHERE id=?",
                (number, timestamp, document_id),
            )
        return self.read(document_id)

    def set_lifecycle(self, document_id, lifecycle):
        with self.transaction(immediate=True):
            row = self.read(document_id)
            if row is None:
                raise KeyError(document_id)
            if row["lifecycle"] == "deleted" and lifecycle != "deleted":
                raise Conflict("Restoring deleted documents requires an explicit restore operation")
            self.conn.execute("UPDATE documents SET lifecycle=?,updated_at=? WHERE id=?",
                              (lifecycle, now(), document_id))
            if lifecycle == "deleted":
                self.record_deletion(document_id, now(), {"origin": "serein"})

    def set_manual_surface(self, document_id, allowed):
        if allowed is not None and type(allowed) is not bool:
            raise ValueError("allowed must be True, False, or None")
        cursor = self.conn.execute("UPDATE documents SET manual_surface=? WHERE id=?",
                                   (allowed, document_id))
        if cursor.rowcount != 1:
            raise KeyError(document_id)

    def add_source(self, source_key, content, *, metadata=None):
        if not source_key or not content.strip():
            raise ValueError("Evidence needs a source identity and nonempty content")
        sha = digest(content)
        source_id = "source_" + digest(encode([source_key, sha]))
        self.conn.execute("INSERT OR IGNORE INTO sources VALUES (?, ?, ?, ?, ?)",
                          (source_id, source_key, content, sha, encode(metadata or {})))
        return source_id

    def bind(self, document_id, source_id, *, binding_id=None, metadata=None,
             active=True, record_action=True, actor="user"):
        binding_id = binding_id or "binding_" + digest(encode([document_id, source_id]))
        with self.transaction(immediate=True):
            old = self.conn.execute("SELECT * FROM evidence_bindings WHERE id=?",
                                    (binding_id,)).fetchone()
            if old and (old["document_id"] != document_id or old["source_id"] != source_id):
                raise Conflict("Evidence binding ID belongs to another document or snapshot")
            self.conn.execute(
                "INSERT INTO evidence_bindings VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET active=excluded.active,metadata_json=excluded.metadata_json",
                (binding_id, document_id, source_id, active, encode(metadata or {})),
            )
            if record_action:
                self.add_evidence_action(binding_id, "bind" if active else "unbind", actor=actor)
        return binding_id

    def add_evidence_action(self, binding_id, action, *, actor="", action_id=None,
                            occurred_at=None, metadata=None):
        self.conn.execute("INSERT INTO evidence_actions VALUES (?, ?, ?, ?, ?, ?)",
                          (action_id or uuid4().hex, binding_id, action, actor,
                           occurred_at or now(), encode(metadata or {})))

    def unbind(self, binding_id, *, actor="user"):
        with self.transaction():
            cursor = self.conn.execute("UPDATE evidence_bindings SET active=0 WHERE id=?", (binding_id,))
            if cursor.rowcount != 1:
                raise KeyError(binding_id)
            self.add_evidence_action(binding_id, "unbind", actor=actor)

    def surface_state(self, document_id):
        document = self.read(document_id)
        if not document:
            raise KeyError(document_id)
        reasons = []
        if document['metadata'].get('memory_role') == 'feel':
            from .feelings import surface_reasons
            reasons.extend(surface_reasons(self, document))
        if document["lifecycle"] != "active":
            reasons.append(document["lifecycle"])
        if document["manual_surface"] != 1:
            reasons.append("manual_disabled" if document["manual_surface"] == 0 else "manual_unreviewed")
        if document["kind"] == "narrative":
            reasons.append("narrative_requires_intent")
        covering = []
        if document["kind"] == "event":
            if self.promoted_scene(document_id):
                reasons.append("promoted_to_scene")
            if self.conn.execute("SELECT 1 FROM event_replacements WHERE predecessor_id=?",
                                 (document_id,)).fetchone():
                reasons.append("replaced_by_event")
            # One active Scene must contain every exact, active source snapshot.
            # Several partial Scenes are not silently combined into full coverage.
            covering = [row[0] for row in self.conn.execute(
                "SELECT s.id FROM documents s WHERE s.kind='scene' AND s.lifecycle='active' "
                "AND EXISTS (SELECT 1 FROM evidence_bindings e WHERE e.document_id=? AND e.active=1) "
                "AND NOT EXISTS (SELECT 1 FROM evidence_bindings e WHERE e.document_id=? AND e.active=1 "
                "AND NOT EXISTS (SELECT 1 FROM evidence_bindings b WHERE b.document_id=s.id "
                "AND b.active=1 AND b.source_id=e.source_id)) ORDER BY s.id",
                (document_id, document_id),
            )]
            if covering:
                reasons.append("covered_by_scene")
        return {"document_id": document_id, "can_surface": not reasons,
                "reasons": reasons, "covering_scene_ids": covering}

    def promoted_scene(self, event_id):
        scene = self.read(promoted_scene_id(event_id))
        if (scene and scene["kind"] == "scene" and scene["lifecycle"] != "deleted"
                and scene["metadata"].get("promoted_from_event", {}).get("id") == event_id):
            return scene
        return None

    def record_deletion(self, document_id, deleted_at, metadata):
        with self.transaction():
            self.conn.execute(
                "INSERT INTO deletions VALUES (?, ?, ?) ON CONFLICT(document_id) DO NOTHING",
                (document_id, deleted_at, encode(metadata)),
            )
            self.conn.execute("UPDATE documents SET lifecycle='deleted' WHERE id=?", (document_id,))

    def save_import_record(self, origin, path, content):
        sha = digest(content)
        old = self.conn.execute("SELECT sha256 FROM import_records WHERE origin=? AND path=?",
                                (origin, path)).fetchone()
        if old:
            if old[0] != sha:
                raise Conflict(f"An imported source changed: {path}")
            return False
        self.conn.execute("INSERT INTO import_records VALUES (?, ?, ?, ?, ?)",
                          (origin, path, sha, content, now()))
        return True
