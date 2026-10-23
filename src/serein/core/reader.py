"""Explicit object and Narrative-material reads from an existing read-only DB."""

import base64
import json
import re

from .notebook import read_entry, resolve_entry
from .store import Store


KINDS = {"scene", "event", "narrative", "diary", "darkroom", "upload", "shadow", "dream"}


class Reader:
    def __init__(self, database):
        self.store = Store(database, read_only=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.store.close()

    def _identity(self, identifier, kind):
        target = str(identifier)
        prefix, separator, tail = target.partition(":")
        if separator and prefix in KINDS:
            if kind and kind != prefix:
                raise ValueError("Explicit kind conflicts with the typed ID")
            kind, target = prefix, tail
        if kind is None:
            row = self.store.conn.execute("SELECT kind FROM documents WHERE id=?", (target,)).fetchone()
            if row:
                kind = row[0]
            elif target.startswith("upload_"):
                kind = "upload"
            elif target.startswith(("window_", "dream_")):
                kind = "shadow" if target.startswith("window_") else "dream"
            elif re.fullmatch(r"(?:diary-vps-)?\d+", target) or target.startswith(("diary_source_", "dr_", "legacy_darkroom:")):
                kind = resolve_entry(self.store, target).get("kind", "diary")
            else:
                kind = next((k for k in ("scene", "event", "narrative") if target.startswith(k + "_")), None)
        if kind is not None and kind not in KINDS:
            raise ValueError(f"Unsupported object kind: {kind}")
        return kind, target

    @staticmethod
    def _result(kind, target, status, **extra):
        return {"kind": kind, "id": target, "status": status, "readable": False,
                "document": None, "evidence": [], "comments": [], **extra}

    def read(self, identifier, *, kind=None, revision=None, with_evidence=True, include_blob=False, at=None):
        kind, target = self._identity(identifier, kind)
        if kind in {"shadow", "dream"}:
            if self.store.conn.execute("SELECT 1 FROM deletions WHERE document_id=?", (target,)).fetchone():
                return self._result(kind, target, "deleted")
            if self.store.conn.execute("PRAGMA user_version").fetchone()[0] < 6:
                return self._result(kind, target, "archive_not_imported")
            row = self.store.conn.execute("SELECT * FROM historical_works WHERE id=?", (target,)).fetchone()
            if not row:
                return self._result(kind, target, "missing")
            if row["kind"] != kind:
                return self._result(kind, target, "wrong_kind")
            if revision is not None and revision != row["revision"]:
                return self._result(kind, target, "revision_missing")
            return self._result(kind, target, "historical", readable=True,
                                document={"title": row["title"], "body_md": row["body_md"], "revision": row["revision"],
                                          "body_sha256": row["body_sha256"], "metadata": json.loads(row["metadata_json"])},
                                surface_state={"can_surface": False, "reasons": ["historical_archive"]},
                                evidence_scope="legacy_references_only")
        if kind in {"diary", "darkroom"}:
            result = read_entry(self.store, target, kind=kind, at=at)
            base = self._result(kind, str(result["entry_id"]) if result["entry_id"] is not None else target,
                                result["resolution"], requested_id=target)
            if not result["entry"]:
                return base
            entry, comments = result["entry"], result["comments"]
            historical = revision is not None and revision != entry["revision"]
            if historical:
                rows = self.store.conn.execute("SELECT metadata_json FROM diary_history WHERE entry_id=? AND revision=?",
                                               (entry["id"], revision)).fetchall()
                if len(rows) != 1:
                    return {**base, "status": "revision_missing" if not rows else "ambiguous_revision"}
                entry = json.loads(rows[0][0])
                # Current deletion/locking was checked above; old snapshot
                # restrictions must also remain in force, not expose old locks.
                if entry.get("visibility", "active") != "active":
                    return {**base, "status": entry["visibility"]}
                if entry.get("unlock_at"):
                    from datetime import datetime, timezone
                    unlock = datetime.fromisoformat(entry["unlock_at"])
                    clock = datetime.fromisoformat(at) if isinstance(at, str) else at or datetime.now(timezone.utc)
                    if unlock.tzinfo is None or clock.tzinfo is None:
                        return {**base, "status": "unresolved_unlock_time"}
                    if unlock > clock:
                        return {**base, "status": "locked"}
                comments = []  # Old comment membership was not versioned.
            return {**base, "readable": True,
                    "document": {"title": entry.get("title"), "body_md": entry["content"],
                                 "author": entry["author"], "revision": entry["revision"], "metadata": entry},
                    "comments": comments, "comments_scope": "unavailable_for_revision" if historical else "current"}
        if kind == "upload":
            if revision is not None:
                return self._result(kind, target, "revision_unsupported")
            row = self.store.conn.execute(
                "SELECT u.metadata_json,r.content,r.sha256 FROM narrative_uploads u JOIN import_records r "
                "ON r.origin=u.origin AND r.path=u.path WHERE u.id=?", (target,)).fetchone()
            if row is None:
                return self._result(kind, target, "missing")
            meta = json.loads(row["metadata_json"])
            result = self._result(kind, target, "available", readable=True,
                                  document={"title": meta.get("filename"), "body_md": meta.get("extracted_text", ""),
                                            "body_format": "extracted_text", "metadata": meta},
                                  original_available=True, original_sha256=row["sha256"])
            if include_blob:
                result["bytes_b64"] = base64.b64encode(row["content"]).decode("ascii")
            return result

        current = self.store.read(target)
        if current is None:
            deleted = self.store.conn.execute("SELECT 1 FROM deletions WHERE document_id=?", (target,)).fetchone()
            return self._result(kind, target, "deleted" if deleted else "missing")
        if kind and kind != current["kind"]:
            return self._result(kind, target, "wrong_kind")
        kind = current["kind"]
        if current["lifecycle"] == "deleted":
            return self._result(kind, target, "deleted")
        document = current if revision is None else self.store.read(target, revision=revision)
        if document is None:
            return self._result(kind, target, "revision_missing")
        historical = document["body_revision"] != current["revision"]
        result = self._result(kind, target, current["lifecycle"], readable=True,
                              document=document, surface_state=self.store.surface_state(target),
                              evidence_scope="unavailable_for_revision" if historical else "current_bindings")
        if self.store.conn.execute('PRAGMA user_version').fetchone()[0]>=9:
            result['annotations']=[{'id':r['key'],**json.loads(r['payload_json']),'created_at':r['created_at']}
                for r in self.store.conn.execute("SELECT * FROM personal_records WHERE scope='annotation' AND document_id=? AND deleted=0 ORDER BY created_at,key",(target,))]
            result['annotations_scope']='current_personal_notes_not_evidence'
        if with_evidence and not historical:
            result["evidence"] = [{"binding_id": row["id"], "source_id": row["source_id"],
                                   "source_key": row["source_key"], "content": row["content"],
                                   "content_sha256": row["content_sha256"], "metadata": json.loads(row["metadata_json"])}
                                  for row in self.store.conn.execute(
                                      "SELECT b.*,s.source_key,s.content,s.content_sha256 FROM evidence_bindings b "
                                      "JOIN sources s ON s.id=b.source_id WHERE b.document_id=? AND b.active=1 ORDER BY b.id", (target,))]
        return result

    def _material_references(self, document):
        """Current or historical references, before resolving/reading targets."""
        number = document["body_revision"]
        references = [dict(r) for r in self.store.conn.execute(
            "SELECT kind,target_id,disposition,locator,metadata_json FROM narrative_materials "
            "WHERE document_id=? AND revision=? ORDER BY kind,target_id,locator", (document["id"], number))]
        # New Arc appends apply to current material reading, not retroactively
        # to an old Narrative version. Explicit exclusions still win below.
        if number == document["revision"]:
            arc = document["metadata"].get("legacy_registry", document["metadata"]).get("arc_key")
            if arc:
                references.extend({"kind": "event", "target_id": row["event_id"], "disposition": "appended",
                                   "locator": "event_arc_links", "metadata_json": row["metadata_json"]}
                                  for row in self.store.conn.execute("SELECT * FROM event_arc_links WHERE arc_key=? ORDER BY event_id", (arc,)))
        return references

    def materials(self, identifier, *, revision=None, include_mentions=False, with_evidence=False,
                  offset=0, limit=20, at=None):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative and limit must be 1..100")
        owner = self.read(identifier, kind="narrative", revision=revision, with_evidence=False, at=at)
        if not owner["readable"]:
            return {"narrative_id": owner["id"], "status": owner["status"], "items": [], "total": 0, "next_offset": None}
        document = owner["document"]
        number = document["body_revision"]
        references = self._material_references(document)
        grouped = {}
        for ref in references:
            target = ref["target_id"]
            if ref["kind"] in {"diary", "darkroom"}:
                resolved = resolve_entry(self.store, target, kind=ref["kind"], at=at)
                if resolved["entry_id"] is not None:
                    target = str(resolved["entry_id"])
            key = ref["kind"], target
            group = grouped.setdefault(key, {"kind": ref["kind"], "id": target, "requested_ids": [], "references": []})
            if ref["target_id"] not in group["requested_ids"]:
                group["requested_ids"].append(ref["target_id"])
            provenance = {"disposition": ref["disposition"], "locator": ref["locator"],
                          "target_id": ref["target_id"], "metadata": json.loads(ref["metadata_json"])}
            if provenance not in group["references"]:
                group["references"].append(provenance)
        ordered = [grouped[k] for k in sorted(grouped)]
        items = []
        for group in ordered[offset:offset + limit]:
            dispositions = {r["disposition"] for r in group["references"]}
            if "excluded" in dispositions:
                items.append({**group, "selection": "excluded", "object": None})
                continue
            if not dispositions & {"linked", "appended"} and not include_mentions:
                items.append({**group, "selection": "mention_only", "object": None})
                continue
            # Keep a source alias when reading: it can bind an exact historical
            # snapshot and must not be replaced with an unrestricted numeric ID.
            requested = next((i for i in group["requested_ids"] if i.startswith("diary_source_")), group["requested_ids"][0])
            result = self.read(requested, kind=group["kind"], with_evidence=with_evidence, at=at)
            items.append({**group, "selection": "selected", "object": result})
        return {"narrative_id": owner["id"], "revision": number, "status": owner["status"], "items": items,
                "total": len(ordered), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(ordered) else None,
                "target_versions": "current_or_explicit_source_snapshot"}
