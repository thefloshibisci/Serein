from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Any




_SCENE_ID_RE = re.compile(r"\bscene_[A-Za-z0-9_]+\b")
_EVENT_ID_RE = re.compile(r"\bevent_[0-9a-f]{24}\b")
_DIARY_ID_RE = re.compile(r"\bdiary:(\d{1,9})\b")
_DARKROOM_ID_RE = re.compile(r"\bdarkroom:(\d{1,9})\b")
_UPLOAD_ID_RE = re.compile(r"\bupload_[0-9a-f]{32}\b")
_NARRATIVE_ID_RE = re.compile(r"^narrative_[A-Za-z0-9_.:-]{1,96}$")
_ARC_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[^\s\x00-\x1f\x7f]{1,127}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BODY_HEADING_RE = re.compile(r"(?m)^## 第一人称叙事\s*$")
_NEXT_HEADING_RE = re.compile(r"(?m)^##\s+")
_MATERIAL_SNAPSHOT_RE = re.compile(
    r"(?ms)^## 绑定材料快照\s*\n.*?(?=^##\s+|\Z)"
)
_COMPACT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)

_EXACT_EVIDENCE_MARKERS = (
    "原话",
    "原文",
    "逐字",
    "当时怎么说",
    "具体怎么说",
    "谁说",
    "谁喊",
    "哪天",
    "什么时候",
    "几月几日",
    "具体日期",
    "什么型号",
    "型号是什么",
    "具体型号",
    "哪首歌",
    "歌名",
    "哪一首",
)

NARRATIVE_COLLECTION_QUERY_ALIASES = frozenset(
    {"叙事卷", "narrative", "narrativeroll", "narrativerolls"}
)


def _compact(value: Any) -> str:
    return _COMPACT_RE.sub("", str(value or "").strip().lower())


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_body(document: str) -> str:
    match = _BODY_HEADING_RE.search(document)
    if match is None:
        return ""
    start = match.end()
    next_heading = _NEXT_HEADING_RE.search(document, start)
    end = next_heading.start() if next_heading is not None else len(document)
    return document[start:end].strip()


def replace_narrative_body(document: str, body: str) -> str:
    """Replace only the authored body section and preserve the source ledger verbatim."""

    exact_document = str(document or "")
    exact_body = str(body or "").strip()
    if not exact_body:
        raise ValueError("Narrative body is required")
    match = _BODY_HEADING_RE.search(exact_document)
    if match is None:
        raise ValueError("Narrative document has no first-person body section")
    next_heading = _NEXT_HEADING_RE.search(exact_document, match.end())
    end = next_heading.start() if next_heading is not None else len(exact_document)
    suffix = exact_document[end:]
    return exact_document[: match.end()] + "\n\n" + exact_body + "\n\n" + suffix.lstrip("\n")


def replace_narrative_material_snapshot(document: str, snapshot: str) -> str:
    """Replace only the host-managed membership ledger appended to a revision."""

    exact_document = _MATERIAL_SNAPSHOT_RE.sub("", str(document or "")).rstrip()
    exact_snapshot = str(snapshot or "").strip()
    if not exact_snapshot:
        raise ValueError("Narrative material snapshot is required")
    return f"{exact_document}\n\n{exact_snapshot}\n"


def _query_requests_exact_evidence(query: str) -> bool:
    compact_query = _compact(query)
    return any(_compact(marker) in compact_query for marker in _EXACT_EVIDENCE_MARKERS)


def normalize_arc_key(value: Any) -> str:
    key = str(value or "").strip()
    return key if _ARC_KEY_RE.fullmatch(key) else ""




class NarrativeRollStore:

    """Germany validation/rendering with explicit SQLite persistence hooks."""

    @staticmethod
    def _light(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "narrative_id",
                "arc_key",
                "parent_narrative_id",
                "revision",
                "title",
                "title_aliases",
                "scope",
                "publication_status",
                "lifecycle",
                "time_start",
                "time_end",
                "primary_entities",
                "supporting_entities",
                "intent_tags",
                "query_cues",
                "current_status_cue",
                "published_at",
                "source_file",
                "document_sha256",
                "actual_document_sha256",
                "body_sha256",
                "body_chars",
                "linked_scene_count",
                "linked_event_count",
                "linked_diary_ids",
                "linked_diary_count",
                "linked_darkroom_ids",
                "linked_darkroom_count",
                "linked_upload_ids",
                "linked_upload_count",
                "excluded_scene_ids",
                "excluded_event_ids",
                "excluded_diary_ids",
                "excluded_darkroom_ids",
                "excluded_upload_ids",
                "integrity_status",
            )
        }

    @staticmethod
    def _read_card(item: dict[str, Any]) -> dict[str, Any]:
        publication_status = str(item.get("publication_status") or "reviewed")
        member_count = sum(
            int(item.get(field) or 0)
            for field in (
                "linked_scene_count",
                "linked_event_count",
                "linked_diary_count",
                "linked_darkroom_count",
                "linked_upload_count",
            )
        )
        return {
            "arc_key": str(item.get("arc_key") or ""),
            "narrative_id": str(item.get("narrative_id") or ""),
            "title": str(item.get("title") or ""),
            "publication_status": publication_status,
            "revision": int(item.get("revision") or 0),
            "member_count": member_count,
            "narrative_available": bool(
                publication_status in {"reviewed", "published"}
                and int(item.get("body_chars") or 0) > 0
            ),
            "read_hint": "可按需读取",
        }

    @staticmethod
    def _query_card_match(item: dict[str, Any], query: str) -> tuple[float, str]:
        query_key = _compact(query)
        if not query_key:
            return 0.0, ""

        title_values = [
            str(item.get("title") or ""),
            *(str(value or "") for value in item.get("title_aliases", []) or []),
        ]
        entity_values = [
            *(str(value or "") for value in item.get("primary_entities", []) or []),
            *(str(value or "") for value in item.get("supporting_entities", []) or []),
        ]
        title_keys = [key for key in (_compact(value) for value in title_values) if key]
        entity_keys = [key for key in (_compact(value) for value in entity_values) if key]

        if query_key in title_keys:
            return 1.0, "exact_title"
        if any(key in query_key or query_key in key for key in title_keys):
            return 0.95, "title_contains"
        if query_key in entity_keys:
            return 0.9, "exact_entity"
        if any(key in query_key or query_key in key for key in entity_keys):
            return 0.84, "entity_contains"

        fuzzy = (
            max(
                (SequenceMatcher(None, query_key, key).ratio() for key in title_keys),
                default=0.0,
            )
            if len(query_key) >= 4
            else 0.0
        )
        return (fuzzy, "fuzzy_title") if fuzzy >= 0.62 else (0.0, "")

    def find_arc_cards(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Find active Arc identities without reading Narrative prose."""

        safe_query = " ".join(str(query or "").split()).strip()
        safe_limit = max(1, min(int(limit or 5), 10))
        if not safe_query:
            return {
                "status": "invalid",
                "reason": "query_required",
                "query": safe_query,
                "items": [],
                "count": 0,
            }
        matches: list[dict[str, Any]] = []
        for item in self._load():
            if item.get("integrity_status") != "ok":
                continue
            if str(item.get("lifecycle") or "active") != "active":
                continue
            if not str(item.get("arc_key") or ""):
                continue
            score, reason = self._query_card_match(item, safe_query)
            if score <= 0:
                continue
            matches.append(
                {
                    **self._read_card(item),
                    "match_reason": reason,
                    "match_score": round(score, 4),
                }
            )
        matches.sort(
            key=lambda row: (
                -float(row.get("match_score") or 0.0),
                str(row.get("title") or ""),
                str(row.get("arc_key") or ""),
            )
        )
        return {
            "status": "ok",
            "mode": "arc_card_query",
            "query": safe_query,
            "count": len(matches),
            "items": matches[:safe_limit],
            "body_included": False,
            "writes_performed": [],
        }

    def arc_card_by_key(self, arc_key: str) -> dict[str, Any]:
        safe_key = normalize_arc_key(arc_key)
        if not safe_key:
            return {"status": "invalid", "reason": "invalid_arc_key", "arc_key": str(arc_key or "")}
        matches = [
            item
            for item in self._load()
            if str(item.get("arc_key") or "") == safe_key
            and str(item.get("lifecycle") or "active") == "active"
            and item.get("integrity_status") == "ok"
        ]
        if not matches:
            return {"status": "not_found", "arc_key": safe_key}
        if len(matches) != 1:
            return {"status": "invalid", "reason": "duplicate_arc_key", "arc_key": safe_key}
        return {"status": "ok", "item": self._read_card(matches[0]), "body_included": False}

    def arc_material_profile_by_key(self, arc_key: str) -> dict[str, Any]:
        """Return one Arc's body-free identity and persisted material IDs."""

        safe_key = normalize_arc_key(arc_key)
        if not safe_key:
            return {
                "status": "invalid",
                "reason": "invalid_arc_key",
                "arc_key": str(arc_key or "").strip(),
            }
        matches = [
            item
            for item in self._load()
            if str(item.get("arc_key") or "") == safe_key
            and str(item.get("lifecycle") or "active") == "active"
            and item.get("integrity_status") == "ok"
        ]
        if not matches:
            return {"status": "not_found", "arc_key": safe_key}
        if len(matches) != 1:
            return {
                "status": "invalid",
                "reason": "duplicate_arc_key",
                "arc_key": safe_key,
            }
        item = matches[0]
        return {
            "status": "ok",
            "arc_key": safe_key,
            "card": self._read_card(item),
            "linked_scene_ids": list(item.get("linked_scene_ids") or []),
            "linked_event_ids": list(item.get("linked_event_ids") or []),
            "linked_diary_ids": list(item.get("linked_diary_ids") or []),
            "linked_darkroom_count": len(item.get("linked_darkroom_ids") or []),
            "linked_upload_ids": list(item.get("linked_upload_ids") or []),
            "linked_upload_count": len(item.get("linked_upload_ids") or []),
            "body_included": False,
        }

    def recall_scope_profiles(self) -> list[dict[str, Any]]:
        """Return body-free Arc metadata for rebuildable recall sidecars."""

        profiles: list[dict[str, Any]] = []
        for item in self._load():
            if item.get("integrity_status") != "ok":
                continue
            if str(item.get("lifecycle") or "active") == "retired":
                continue
            arc_key = str(item.get("arc_key") or "").strip()
            if not arc_key:
                continue
            members = [
                *(
                    {"owner_kind": "scene", "owner_id": str(scene_id)}
                    for scene_id in item.get("linked_scene_ids") or []
                    if str(scene_id or "").strip()
                ),
                *(
                    {"owner_kind": "event", "owner_id": str(event_id)}
                    for event_id in item.get("linked_event_ids") or []
                    if str(event_id or "").strip()
                ),
            ]
            profiles.append(
                {
                    "arc_key": arc_key,
                    "narrative_id": str(item.get("narrative_id") or ""),
                    "title": str(item.get("title") or ""),
                    "title_aliases": list(item.get("title_aliases") or []),
                    "primary_entities": list(item.get("primary_entities") or []),
                    "supporting_entities": list(item.get("supporting_entities") or []),
                    "members": members,
                    "card": self._read_card(item),
                }
            )
        return profiles

    def list(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 20), 100))
        compact_query = _compact(query)
        if compact_query in NARRATIVE_COLLECTION_QUERY_ALIASES:
            compact_query = ""
        rows = []
        for item in self._load():
            searchable = _compact(
                " ".join(
                    [
                        str(item.get("title") or ""),
                        *(str(value or "") for value in item.get("title_aliases", []) or []),
                        *(str(value or "") for value in item.get("primary_entities", []) or []),
                        *(str(value or "") for value in item.get("supporting_entities", []) or []),
                        *(str(value or "") for value in item.get("query_cues", []) or []),
                    ]
                )
            )
            if compact_query and compact_query not in searchable and searchable not in compact_query:
                continue
            rows.append(self._light(item))
        return {
            "status": "ok",
            "mode": "narrative_roll_index",
            "query": str(query or ""),
            "count": len(rows),
            "items": rows[:safe_limit],
            "live_injection_enabled": self.live_injection_enabled,
        }

    def revision_targets(self) -> list[dict[str, Any]]:
        """Return reviewed routing metadata for the derived revision inbox.

        This intentionally excludes Narrative bodies and entity labels.  New
        sources may be offered for review only through title/query cues already
        authored on the roll, never through generic people or Word Map terms.
        """

        return [
            {
                key: item.get(key)
                for key in (
                    "narrative_id",
                    "revision",
                    "title",
                    "title_aliases",
                    "query_cues",
                    "current_status_cue",
                    "published_at",
                    "document_sha256",
                    "integrity_status",
                )
            }
            for item in self._load()
            if str(item.get("lifecycle") or "active") == "active"
            and str(item.get("publication_status") or "reviewed") in {"reviewed", "published"}
        ]

    def resolve_read_query(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        """Read one exact roll identity, otherwise return the bounded index."""

        compact_query = _compact(query)
        if not compact_query or compact_query in NARRATIVE_COLLECTION_QUERY_ALIASES:
            return self.list(query=query, limit=limit)

        exact_matches = []
        for item in self._load():
            identities = (
                item.get("narrative_id"),
                item.get("title"),
                *(item.get("title_aliases", []) or []),
            )
            if compact_query in {_compact(value) for value in identities if value}:
                exact_matches.append(item)

        if len(exact_matches) == 1:
            return self.read(str(exact_matches[0].get("narrative_id") or ""))
        return self.list(query=query, limit=limit)

    def read(self, narrative_id: str) -> dict[str, Any]:
        safe_id = str(narrative_id or "").strip()
        item = next(
            (row for row in self._load() if str(row.get("narrative_id") or "") == safe_id),
            None,
        )
        if item is None:
            return {"status": "not_found", "narrative_id": safe_id}
        if item.get("integrity_status") != "ok":
            return {
                "status": "invalid",
                "narrative_id": safe_id,
                "integrity_status": item.get("integrity_status"),
                "source_file": item.get("source_file"),
                "expected_document_sha256": item.get("document_sha256"),
                "actual_document_sha256": item.get("actual_document_sha256"),
            }
        return {
            "status": "ok",
            "mode": "narrative_roll_full_read",
            **self._light(item),
            "linked_scene_ids": list(item.get("linked_scene_ids") or []),
            "linked_event_ids": list(item.get("linked_event_ids") or []),
            "linked_diary_ids": list(item.get("linked_diary_ids") or []),
            "linked_darkroom_ids": list(item.get("linked_darkroom_ids") or []),
            "linked_upload_ids": list(item.get("linked_upload_ids") or []),
            "history": [
                {
                    **history_item,
                    "linked_scene_ids": list(history_item.get("linked_scene_ids") or []),
                    "linked_event_ids": list(history_item.get("linked_event_ids") or []),
                    "linked_diary_ids": list(history_item.get("linked_diary_ids") or []),
                    "linked_darkroom_ids": list(history_item.get("linked_darkroom_ids") or []),
                    "linked_upload_ids": list(history_item.get("linked_upload_ids") or []),
                }
                for history_item in item.get("history", []) or []
                if isinstance(history_item, dict)
            ],
            "body": str(item.get("body") or ""),
            "full_document": str(item.get("full_document") or ""),
            "reading_boundary": (
                "This collecting Arc is a source index, not authored narrative or original evidence. "
                "Read its linked Scene/Event/raw, Diary, and Darkroom sources selectively."
                if str(item.get("publication_status") or "") == "collecting"
                else "Narrative Roll is a sourced first-person projection, not original evidence. "
                "For exact dates or wording, read its linked Scene/Event/raw, Diary, and Darkroom sources."
            ),
        }

    def read_by_arc_key(self, arc_key: str) -> dict[str, Any]:
        """Resolve one active, intact Arc from its persisted stable key."""

        safe_key = normalize_arc_key(arc_key)
        if not safe_key:
            return {"status": "invalid", "reason": "invalid_arc_key", "arc_key": str(arc_key or "").strip()}
        matches = [
            item
            for item in self._load()
            if str(item.get("arc_key") or "") == safe_key
            and str(item.get("lifecycle") or "active") == "active"
        ]
        if not matches:
            return {"status": "not_found", "arc_key": safe_key}
        if len(matches) != 1:
            return {"status": "invalid", "reason": "duplicate_arc_key", "arc_key": safe_key}
        return self.read(str(matches[0].get("narrative_id") or ""))

    def save_body(
        self,
        narrative_id: str,
        body: str,
        *,
        expected_revision: int,
        expected_document_sha256: str,
        source_scene_ids: list[str] | None = None,
        source_event_ids: list[str] | None = None,
        source_diary_ids: list[int] | None = None,
        source_darkroom_ids: list[int] | None = None,
        source_upload_ids: list[str] | None = None,
        material_snapshot: str = "",
    ) -> dict[str, Any]:
        """Publish one body revision, preserving membership unless an exact proposal is supplied."""

        current = self.read(str(narrative_id or "").strip())
        if current.get("status") != "ok":
            return current
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError):
            return {"status": "invalid", "reason": "invalid_expected_revision"}
        if expected != int(current.get("revision") or 0):
            return {
                "status": "conflict",
                "reason": "revision_mismatch",
                "current_revision": int(current.get("revision") or 0),
            }
        expected_hash = str(expected_document_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            return {"status": "invalid", "reason": "invalid_expected_document_sha256"}
        if expected_hash != str(current.get("document_sha256") or "").strip().lower():
            return {
                "status": "conflict",
                "reason": "document_hash_mismatch",
                "current_document_sha256": str(current.get("document_sha256") or ""),
            }
        try:
            document = replace_narrative_body(current.get("full_document") or "", body)
            membership_proposal_supplied = any(
                value is not None
                for value in (
                    source_scene_ids,
                    source_event_ids,
                    source_diary_ids,
                    source_darkroom_ids,
                    source_upload_ids,
                )
            )
            if membership_proposal_supplied:
                document = replace_narrative_material_snapshot(document, material_snapshot)
        except ValueError as exc:
            return {"status": "invalid", "reason": str(exc)}

        target_scene_ids = list(current.get("linked_scene_ids") or []) if source_scene_ids is None else list(source_scene_ids)
        target_event_ids = list(current.get("linked_event_ids") or []) if source_event_ids is None else list(source_event_ids)
        target_diary_ids = list(current.get("linked_diary_ids") or []) if source_diary_ids is None else list(source_diary_ids)
        target_darkroom_ids = list(current.get("linked_darkroom_ids") or []) if source_darkroom_ids is None else list(source_darkroom_ids)
        target_upload_ids = list(current.get("linked_upload_ids") or []) if source_upload_ids is None else list(source_upload_ids)
        membership_changed = (
            target_scene_ids != list(current.get("linked_scene_ids") or [])
            or target_event_ids != list(current.get("linked_event_ids") or [])
            or target_diary_ids != list(current.get("linked_diary_ids") or [])
            or target_darkroom_ids != list(current.get("linked_darkroom_ids") or [])
            or target_upload_ids != list(current.get("linked_upload_ids") or [])
        )
        excluded_scene_ids = [item for item in self.source_scene_ids(document) if item not in target_scene_ids]
        excluded_event_ids = [item for item in self.source_event_ids(document) if item not in target_event_ids]
        excluded_diary_ids = [item for item in self.source_diary_ids(document) if item not in target_diary_ids]
        excluded_darkroom_ids = [item for item in self.source_darkroom_ids(document) if item not in target_darkroom_ids]
        excluded_upload_ids = [item for item in self.source_upload_ids(document) if item not in target_upload_ids]
        # A body-only edit must retain withdrawals no longer present in the
        # material block. Only explicitly selecting that ID may restore it.
        excluded_scene_ids = list(dict.fromkeys([*current.get('excluded_scene_ids', []), *excluded_scene_ids]))
        excluded_event_ids = list(dict.fromkeys([*current.get('excluded_event_ids', []), *excluded_event_ids]))
        excluded_diary_ids = list(dict.fromkeys([*current.get('excluded_diary_ids', []), *excluded_diary_ids]))
        excluded_darkroom_ids = list(dict.fromkeys([*current.get('excluded_darkroom_ids', []), *excluded_darkroom_ids]))
        excluded_upload_ids = list(dict.fromkeys([*current.get('excluded_upload_ids', []), *excluded_upload_ids]))
        if source_scene_ids is not None:excluded_scene_ids = [key for key in excluded_scene_ids if key not in target_scene_ids]
        if source_event_ids is not None:excluded_event_ids = [key for key in excluded_event_ids if key not in target_event_ids]
        if source_diary_ids is not None:excluded_diary_ids = [key for key in excluded_diary_ids if key not in target_diary_ids]
        if source_darkroom_ids is not None:excluded_darkroom_ids = [key for key in excluded_darkroom_ids if key not in target_darkroom_ids]
        if source_upload_ids is not None:excluded_upload_ids = [key for key in excluded_upload_ids if key not in target_upload_ids]

        result = self.publish(
            narrative_id=str(current.get("narrative_id") or ""),
            document=document,
            expected_revision=expected,
            title=str(current.get("title") or ""),
            arc_key=str(current.get("arc_key") or ""),
            parent_narrative_id=str(current.get("parent_narrative_id") or ""),
            source_scene_ids=target_scene_ids,
            source_event_ids=target_event_ids,
            source_diary_ids=target_diary_ids,
            source_darkroom_ids=target_darkroom_ids,
            source_upload_ids=target_upload_ids,
            excluded_scene_ids=excluded_scene_ids,
            excluded_event_ids=excluded_event_ids,
            excluded_diary_ids=excluded_diary_ids,
            excluded_darkroom_ids=excluded_darkroom_ids,
            excluded_upload_ids=excluded_upload_ids,
            title_aliases=list(current.get("title_aliases") or []),
            primary_entities=list(current.get("primary_entities") or []),
            supporting_entities=list(current.get("supporting_entities") or []),
            intent_tags=list(current.get("intent_tags") or []),
            query_cues=list(current.get("query_cues") or []),
            time_start=str(current.get("time_start") or ""),
            time_end=str(current.get("time_end") or ""),
            current_status_cue=str(current.get("current_status_cue") or ""),
            publication_status='reviewed' if current.get('publication_status') == 'collecting' else str(current.get("publication_status") or "reviewed"),
            lifecycle=str(current.get("lifecycle") or "active"),
        )
        if result.get("status") in {"created", "updated"}:
            result["manual_body_save"] = True
            result["membership_changed"] = membership_changed
            result["membership_preserved"] = (
                list(result.get("linked_scene_ids") or []) == list(current.get("linked_scene_ids") or [])
                and list(result.get("linked_event_ids") or []) == list(current.get("linked_event_ids") or [])
                and list(result.get("linked_diary_ids") or []) == list(current.get("linked_diary_ids") or [])
                and list(result.get("linked_darkroom_ids") or []) == list(current.get("linked_darkroom_ids") or [])
                and list(result.get("linked_upload_ids") or []) == list(current.get("linked_upload_ids") or [])
            )
        return result

    @staticmethod
    def source_scene_ids(document: str, explicit_ids: list[str] | None = None) -> list[str]:
        return list(
            dict.fromkeys(
                str(value or "").strip()
                for value in [*(explicit_ids or []), *_SCENE_ID_RE.findall(str(document or ""))]
                if str(value or "").strip()
            )
        )

    @staticmethod
    def source_event_ids(document: str, explicit_ids: list[str] | None = None) -> list[str]:
        return list(
            dict.fromkeys(
                str(value or "").strip()
                for value in [*(explicit_ids or []), *_EVENT_ID_RE.findall(str(document or ""))]
                if str(value or "").strip()
            )
        )

    @staticmethod
    def source_diary_ids(document: str, explicit_ids: list[int] | None = None) -> list[int]:
        values = [*(explicit_ids or []), *_DIARY_ID_RE.findall(str(document or ""))]
        return list(
            dict.fromkeys(
                int(value)
                for value in values
                if str(value or "").strip().isdigit() and int(value) > 0
            )
        )

    @staticmethod
    def source_darkroom_ids(document: str, explicit_ids: list[int] | None = None) -> list[int]:
        values = [*(explicit_ids or []), *_DARKROOM_ID_RE.findall(str(document or ""))]
        return list(
            dict.fromkeys(
                int(value)
                for value in values
                if str(value or "").strip().isdigit() and int(value) > 0
            )
        )

    @staticmethod
    def source_upload_ids(document: str, explicit_ids: list[str] | None = None) -> list[str]:
        return list(
            dict.fromkeys(
                str(value or "").strip()
                for value in [*(explicit_ids or []), *_UPLOAD_ID_RE.findall(str(document or ""))]
                if _UPLOAD_ID_RE.fullmatch(str(value or "").strip())
            )
        )

    @staticmethod
    def _string_list(values: Any, *, limit: int = 40) -> list[str]:
        if isinstance(values, str):
            source = re.split(r"[\n,|]+", values)
        elif isinstance(values, (list, tuple, set)):
            source = values
        else:
            source = []
        return list(
            dict.fromkeys(
                str(value or "").strip()
                for value in source
                if str(value or "").strip()
            )
        )[:limit]

    def publish(
        self,
        *,
        narrative_id: str,
        document: str,
        expected_revision: int,
        title: str,
        arc_key: str = "",
        parent_narrative_id: str = "",
        source_scene_ids: list[str] | None = None,
        source_event_ids: list[str] | None = None,
        source_diary_ids: list[int] | None = None,
        source_darkroom_ids: list[int] | None = None,
        source_upload_ids: list[str] | None = None,
        excluded_scene_ids: list[str] | None = None,
        excluded_event_ids: list[str] | None = None,
        excluded_diary_ids: list[int] | None = None,
        excluded_darkroom_ids: list[int] | None = None,
        excluded_upload_ids: list[str] | None = None,
        title_aliases: list[str] | None = None,
        primary_entities: list[str] | None = None,
        supporting_entities: list[str] | None = None,
        intent_tags: list[str] | None = None,
        query_cues: list[str] | None = None,
        time_start: str = "",
        time_end: str = "",
        current_status_cue: str = "",
        publication_status: str = "reviewed",
        lifecycle: str = "active",
    ) -> dict[str, Any]:
        """Publish one exact authored revision with optimistic concurrency."""

        safe_id = str(narrative_id or "").strip()
        exact_document = str(document or "")
        safe_title = str(title or "").strip()
        if not _NARRATIVE_ID_RE.fullmatch(safe_id):
            return {"status": "invalid", "reason": "invalid_narrative_id", "narrative_id": safe_id}
        if not safe_title:
            return {"status": "invalid", "reason": "title_required", "narrative_id": safe_id}
        if not exact_document.strip():
            return {"status": "invalid", "reason": "document_required", "narrative_id": safe_id}

        publication_status = str(publication_status or "reviewed").strip().lower()
        if publication_status not in {"collecting", "reviewed", "published"}:
            return {
                "status": "invalid",
                "reason": "publication_status_must_be_collecting_reviewed_or_published",
                "narrative_id": safe_id,
            }
        if publication_status != "collecting" and not _extract_body(exact_document):
            return {
                "status": "invalid",
                "reason": "missing_first_person_body",
                "narrative_id": safe_id,
            }

        safe_excluded_scene_ids = self._string_list(excluded_scene_ids, limit=1000)
        safe_excluded_event_ids = self._string_list(excluded_event_ids, limit=1000)
        safe_excluded_diary_ids = list(dict.fromkeys(
            int(value) for value in excluded_diary_ids or [] if int(value) > 0
        ))
        safe_excluded_darkroom_ids = list(dict.fromkeys(
            int(value) for value in excluded_darkroom_ids or [] if int(value) > 0
        ))
        safe_excluded_upload_ids = self._string_list(excluded_upload_ids, limit=1000)
        linked_scene_ids = [item for item in self.source_scene_ids(exact_document, source_scene_ids) if item not in safe_excluded_scene_ids]
        linked_event_ids = [item for item in self.source_event_ids(exact_document, source_event_ids) if item not in safe_excluded_event_ids]
        linked_diary_ids = [item for item in self.source_diary_ids(exact_document, source_diary_ids) if item not in safe_excluded_diary_ids]
        linked_darkroom_ids = [item for item in self.source_darkroom_ids(exact_document, source_darkroom_ids) if item not in safe_excluded_darkroom_ids]
        linked_upload_ids = [item for item in self.source_upload_ids(exact_document, source_upload_ids) if item not in safe_excluded_upload_ids]
        if (
            len(linked_scene_ids)
            + len(linked_event_ids)
            + len(linked_diary_ids)
            + len(linked_darkroom_ids)
            + len(linked_upload_ids)
            < 2
        ):
            return {
                "status": "invalid",
                "reason": (
                    "at_least_two_source_scenes_required"
                    if not linked_event_ids
                    else "at_least_two_sources_required"
                ),
                "narrative_id": safe_id,
            }

        requested_arc_key = str(arc_key or "").strip()
        if requested_arc_key and not normalize_arc_key(requested_arc_key):
            return {
                "status": "invalid",
                "reason": "invalid_arc_key",
                "narrative_id": safe_id,
                "arc_key": requested_arc_key,
            }
        requested_parent_id = str(parent_narrative_id or "").strip()
        if requested_parent_id and not _NARRATIVE_ID_RE.fullmatch(requested_parent_id):
            return {
                "status": "invalid",
                "reason": "invalid_parent_narrative_id",
                "narrative_id": safe_id,
                "parent_narrative_id": requested_parent_id,
            }
        if requested_parent_id == safe_id:
            return {
                "status": "invalid",
                "reason": "parent_narrative_cannot_be_self",
                "narrative_id": safe_id,
            }
        missing_in_document = [scene_id for scene_id in linked_scene_ids if scene_id not in exact_document]
        if missing_in_document:
            return {
                "status": "invalid",
                "reason": "source_scene_id_missing_from_document",
                "narrative_id": safe_id,
                "scene_ids": missing_in_document,
            }
        missing_events_in_document = [
            event_id for event_id in linked_event_ids if event_id not in exact_document
        ]
        if missing_events_in_document:
            return {
                "status": "invalid",
                "reason": "source_event_id_missing_from_document",
                "narrative_id": safe_id,
                "event_ids": missing_events_in_document,
            }
        missing_diaries_in_document = [
            diary_id for diary_id in linked_diary_ids if f"diary:{diary_id}" not in exact_document
        ]
        if missing_diaries_in_document:
            return {
                "status": "invalid",
                "reason": "source_diary_id_missing_from_document",
                "narrative_id": safe_id,
                "diary_ids": missing_diaries_in_document,
            }
        missing_darkrooms_in_document = [
            darkroom_id
            for darkroom_id in linked_darkroom_ids
            if f"darkroom:{darkroom_id}" not in exact_document
        ]
        if missing_darkrooms_in_document:
            return {
                "status": "invalid",
                "reason": "source_darkroom_id_missing_from_document",
                "narrative_id": safe_id,
                "darkroom_ids": missing_darkrooms_in_document,
            }
        missing_uploads_in_document = [
            upload_id for upload_id in linked_upload_ids if upload_id not in exact_document
        ]
        if missing_uploads_in_document:
            return {
                "status": "invalid",
                "reason": "source_upload_id_missing_from_document",
                "narrative_id": safe_id,
                "upload_ids": missing_uploads_in_document,
            }

        lifecycle = str(lifecycle or "active").strip().lower()
        if lifecycle not in {"active", "closed", "retired"}:
            return {
                "status": "invalid",
                "reason": "lifecycle_must_be_active_closed_or_retired",
                "narrative_id": safe_id,
            }
        for label, value in (("time_start", time_start), ("time_end", time_end)):
            if str(value or "").strip() and not _DATE_RE.fullmatch(str(value).strip()):
                return {
                    "status": "invalid",
                    "reason": f"{label}_must_be_yyyy_mm_dd",
                    "narrative_id": safe_id,
                }

        try:
            expected = max(0, int(expected_revision))
        except (TypeError, ValueError):
            return {"status": "invalid", "reason": "invalid_expected_revision", "narrative_id": safe_id}

        raw = self._registry()
        if not isinstance(raw, dict):
            raw = {"schema_version": "narrative-roll-registry-v1", "rolls": []}
        entries = raw.get("rolls") if isinstance(raw.get("rolls"), list) else []
        current_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if isinstance(entry, dict) and str(entry.get("narrative_id") or "") == safe_id
            ),
            None,
        )
        current = entries[current_index] if current_index is not None else None
        current_revision = int((current or {}).get("revision") or 0)
        if current_revision != expected:
            return {
                "status": "conflict",
                "reason": "revision_mismatch",
                "narrative_id": safe_id,
                "expected_revision": expected,
                "current_revision": current_revision,
            }


        current_arc_key = normalize_arc_key((current or {}).get("arc_key"))
        safe_arc_key = normalize_arc_key(requested_arc_key) or current_arc_key
        if publication_status == "collecting" and not safe_arc_key:
            return {
                "status": "invalid",
                "reason": "arc_key_required_for_collecting",
                "narrative_id": safe_id,
            }
        if current_arc_key and requested_arc_key and safe_arc_key != current_arc_key:
            return {
                "status": "conflict",
                "reason": "arc_key_is_stable",
                "narrative_id": safe_id,
                "arc_key": current_arc_key,
            }
        duplicate = next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict)
                and str(entry.get("narrative_id") or "") != safe_id
                and normalize_arc_key(entry.get("arc_key")) == safe_arc_key
            ),
            None,
        ) if safe_arc_key else None
        if duplicate is not None:
            return {
                "status": "conflict",
                "reason": "arc_key_already_exists",
                "narrative_id": safe_id,
                "arc_key": safe_arc_key,
            }

        current_parent_id = str((current or {}).get("parent_narrative_id") or "").strip()
        if current is not None and requested_parent_id and requested_parent_id != current_parent_id:
            return {
                "status": "conflict",
                "reason": "parent_narrative_id_is_stable",
                "narrative_id": safe_id,
                "parent_narrative_id": current_parent_id,
            }
        safe_parent_id = current_parent_id or requested_parent_id
        if safe_parent_id:
            parent_entry = next(
                (
                    entry
                    for entry in entries
                    if isinstance(entry, dict)
                    and str(entry.get("narrative_id") or "").strip() == safe_parent_id
                ),
                None,
            )
            if parent_entry is None:
                return {
                    "status": "invalid",
                    "reason": "parent_narrative_not_found",
                    "narrative_id": safe_id,
                    "parent_narrative_id": safe_parent_id,
                }
            if str(parent_entry.get("lifecycle") or "active").strip().lower() != "active":
                return {
                    "status": "invalid",
                    "reason": "parent_narrative_not_active",
                    "narrative_id": safe_id,
                    "parent_narrative_id": safe_parent_id,
                }

        revision = current_revision + 1
        document_hash = _sha256_text(exact_document)
        published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source_file = f"sqlite:{safe_id}/revision-{revision:04d}"

        history = list((current or {}).get("history", []) or [])
        if current:
            history.append(
                {
                    key: current.get(key)
                    for key in (
                        "revision",
                        "publication_status",
                        "lifecycle",
                        "source_file",
                        "document_sha256",
                        "published_at",
                        "linked_scene_ids",
                        "linked_event_ids",
                        "linked_diary_ids",
                        "linked_darkroom_ids",
                        "linked_upload_ids",
                        "excluded_scene_ids",
                        "excluded_event_ids",
                        "excluded_diary_ids",
                        "excluded_darkroom_ids",
                        "excluded_upload_ids",
                        "arc_key",
                        "parent_narrative_id",
                    )
                }
            )

        entry = {
            "narrative_id": safe_id,
            "arc_key": safe_arc_key,
            "parent_narrative_id": safe_parent_id,
            "revision": revision,
            "scope": "arc",
            "title": safe_title,
            "title_aliases": self._string_list(title_aliases),
            "publication_status": publication_status,
            "lifecycle": lifecycle,
            "time_start": str(time_start or "").strip(),
            "time_end": str(time_end or "").strip(),
            "primary_entities": self._string_list(primary_entities),
            "supporting_entities": self._string_list(supporting_entities),
            "intent_tags": self._string_list(intent_tags),
            "query_cues": self._string_list(query_cues),
            "current_status_cue": str(current_status_cue or "").strip(),
            "source_file": source_file,
            "document_sha256": document_hash,
            "linked_scene_ids": linked_scene_ids,
            "linked_event_ids": linked_event_ids,
            "linked_diary_ids": linked_diary_ids,
            "linked_darkroom_ids": linked_darkroom_ids,
            "linked_upload_ids": linked_upload_ids,
            "excluded_scene_ids": safe_excluded_scene_ids,
            "excluded_event_ids": safe_excluded_event_ids,
            "excluded_diary_ids": safe_excluded_diary_ids,
            "excluded_darkroom_ids": safe_excluded_darkroom_ids,
            "excluded_upload_ids": safe_excluded_upload_ids,
            "published_at": published_at,
            "published_by": f"{self.identity['ai_name']}_manual",
            "history": history,
        }
        if current_index is None:
            entries.append(entry)
        else:
            entries[current_index] = entry
        raw["schema_version"] = str(raw.get("schema_version") or "narrative-roll-registry-v1")
        raw["rolls"] = entries

        self._persist(entry, exact_document)
        result = self.read(safe_id)
        result.update(
            {
                "status": "created" if current is None else "updated",
                "expected_revision": expected,
                "source_scene_ids": linked_scene_ids,
                "source_event_ids": linked_event_ids,
                "source_diary_ids": linked_diary_ids,
                "source_darkroom_ids": linked_darkroom_ids,
                "source_upload_ids": linked_upload_ids,
                "canonical_scene_changed": False,
                "model_called": False,
            }
        )
        return result

    def recall_scope_profiles(self) -> list[dict[str, Any]]:
        """Return body-free Arc metadata for rebuildable recall sidecars."""

        profiles: list[dict[str, Any]] = []
        for item in self._load():
            if item.get("integrity_status") != "ok":
                continue
            if str(item.get("lifecycle") or "active") == "retired":
                continue
            arc_key = str(item.get("arc_key") or "").strip()
            if not arc_key:
                continue
            members = [
                *(
                    {"owner_kind": "scene", "owner_id": str(scene_id)}
                    for scene_id in item.get("linked_scene_ids") or []
                    if str(scene_id or "").strip()
                ),
                *(
                    {"owner_kind": "event", "owner_id": str(event_id)}
                    for event_id in item.get("linked_event_ids") or []
                    if str(event_id or "").strip()
                ),
            ]
            profiles.append(
                {
                    "arc_key": arc_key,
                    "narrative_id": str(item.get("narrative_id") or ""),
                    "title": str(item.get("title") or ""),
                    "title_aliases": list(item.get("title_aliases") or []),
                    "primary_entities": list(item.get("primary_entities") or []),
                    "supporting_entities": list(item.get("supporting_entities") or []),
                    "members": members,
                    "card": self._read_card(item),
                }
            )
        return profiles
