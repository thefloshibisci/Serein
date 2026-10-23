"""Germany HTTP payloads; authentication is supplied by the enclosing router.

Only dependency injection changed. There is no MCP registration in this module.
"""
import re
from base64 import b64decode
from .narrative_materials import (
    material_ids_from_narrative, normalize_material_ids, material_snapshot_sha256,
    narrative_preview_fingerprint, render_material_snapshot,
)
from .narrative_writer_sources import writer_material_ids
from .narrative_uploads import MAX_UPLOAD_BYTES
from .fact_events import FactEventSettlementBlockedError

MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9_.:#-]{1,160}$")

def _int_between(value, default, lower, upper):
    try: return max(lower, min(upper, int(value)))
    except (TypeError, ValueError): return default

class NarrativeHTTP:


    async def _read_narrative_memory(self, narrative_id: str) -> dict:
        """按 narrative_id 只读完整叙事卷：正文、当前状态、来源账、准确性边界、revision 与 linked_scene_ids/linked_event_ids 一起返回。Narrative Roll 是有来源的第一人称派生叙事，不是原始证据；精确日期和逐字原话继续下钻 Scene/Event/raw。"""
        narrative_id = str(narrative_id or "").strip()
        if not narrative_id or not MEMORY_ID_RE.fullmatch(narrative_id):
            return {"status": "invalid", "error": "invalid narrative_id"}
        result = self.rolls.read(narrative_id)
        if result.get("status") != "ok" or not str(result.get("arc_key") or ""):
            return result
        automatic_links = self.events.arc_event_links(result["arc_key"])
        direct_ids = list(result.get("linked_event_ids") or [])
        result["direct_linked_event_ids"] = list(direct_ids)
        automatic_ids = [
            item["event_id"]
            for item in automatic_links
            if item["event_id"] not in direct_ids
        ]
        result["automatic_event_links"] = automatic_links
        result["automatic_linked_event_ids"] = automatic_ids
        result["linked_event_ids"] = [*direct_ids, *automatic_ids]
        result["linked_event_count"] = len(result["linked_event_ids"])
        result["linked_uploads"] = [
            upload
            for upload_id in result.get("linked_upload_ids") or []
            for upload in [self.uploads.read(str(upload_id), include_text=False)]
            if upload.get("status") == "ok"
        ]
        return result

    async def api_narrative_material_upload(self, request):
        """Persist one local file as an immutable Narrative material."""
        from starlette.responses import JSONResponse

        err = None
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "invalid", "reason": "invalid_json", "writes_performed": []}, status_code=400)
        if not isinstance(body, dict) or set(body) != {"filename", "content_type", "content_base64"}:
            return JSONResponse({"status": "invalid", "reason": "invalid_upload_request", "writes_performed": []}, status_code=400)
        encoded = str(body.get("content_base64") or "")
        if len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 8:
            return JSONResponse({"status": "invalid", "reason": "upload_too_large", "max_bytes": MAX_UPLOAD_BYTES, "writes_performed": []}, status_code=413)
        try:
            raw = b64decode(encoded, validate=True)
        except Exception:
            return JSONResponse({"status": "invalid", "reason": "invalid_upload_base64", "writes_performed": []}, status_code=400)
        result = self.uploads.create(
            raw,
            filename=str(body.get("filename") or "upload"),
            content_type=str(body.get("content_type") or "application/octet-stream"),
        )
        status_code = 200 if result.get("status") == "ok" else 409 if result.get("status") == "conflict" else 413 if result.get("reason") == "upload_too_large" else 400
        return JSONResponse(result, status_code=status_code)

    async def api_narrative_rolls(self, request):
        """Read the lightweight Narrative Roll index or one exact full roll."""
        from starlette.responses import JSONResponse

        err = None
        if err:
            return err
        narrative_id = str(request.query_params.get("narrative_id") or "").strip()
        if narrative_id:
            result = await self._read_narrative_memory(narrative_id)
        else:
            result = self.rolls.list(
                query=str(request.query_params.get("query") or ""),
                limit=_int_between(request.query_params.get("limit"), 20, 1, 100),
            )
        status_code = (
            404
            if result.get("status") == "not_found"
            else 400
            if result.get("status") == "invalid"
            else 200
        )
        return JSONResponse(result, status_code=status_code)

    async def api_narrative_roll_preview_input(self, request):
        """Freeze and materialize the exact source-bound input for a host-side Writer preview."""
        from starlette.responses import JSONResponse

        err = None
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "invalid", "reason": "invalid_json_body", "writes_performed": []}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"status": "invalid", "reason": "request_body_not_object", "writes_performed": []}, status_code=400)

        narrative_id = str(body.get("narrative_id") or "").strip()
        mode = str(body.get("mode") or "").strip().lower()
        expected_revision = body.get("expected_revision")
        expected_hash = str(body.get("expected_document_sha256") or "").strip().lower()
        if not narrative_id or not MEMORY_ID_RE.fullmatch(narrative_id) or mode not in {"edit", "update", "rewrite"}:
            return JSONResponse({"status": "invalid", "reason": "invalid_preview_request", "writes_performed": []}, status_code=400)

        narrative = self.rolls.read(narrative_id)
        if narrative.get("status") != "ok":
            return JSONResponse({"status": "not_found", "reason": "narrative_not_found", "writes_performed": []}, status_code=404)
        current_revision = int(narrative.get("revision") or 0)
        current_hash = str(narrative.get("document_sha256") or "").strip().lower()
        if expected_revision is not None and int(expected_revision) != current_revision:
            return JSONResponse({
                "status": "conflict",
                "reason": "narrative_revision_changed",
                "current_revision": current_revision,
                "writes_performed": [],
            }, status_code=409)
        if expected_hash and expected_hash != current_hash:
            return JSONResponse({
                "status": "conflict",
                "reason": "narrative_document_changed",
                "current_document_sha256": current_hash,
                "writes_performed": [],
            }, status_code=409)

        current_material_ids = material_ids_from_narrative(narrative)
        try:
            proposed_material_ids = normalize_material_ids(
                body.get("proposed_material_ids"),
                fallback=current_material_ids,
            )
        except ValueError as exc:
            return JSONResponse({"status": "invalid", "reason": str(exc), "writes_performed": []}, status_code=400)
        proposed_narrative = {
            **narrative,
            "linked_event_ids": proposed_material_ids["event_ids"],
            "linked_scene_ids": proposed_material_ids["scene_ids"],
            "linked_diary_ids": proposed_material_ids["diary_ids"],
            "linked_darkroom_ids": proposed_material_ids["darkroom_ids"],
            "linked_upload_ids": proposed_material_ids["upload_ids"],
        }
        selection = writer_material_ids(
            mode, current_material_ids, proposed_material_ids,
            body_material_ids=self.rolls.body_material_ids(narrative) if mode == "update" else None,
        )
        if selection.get("status") != "ok":
            return JSONResponse({**selection, "writes_performed": []}, status_code=409)
        writer_ids = selection["material_ids"]
        writer_narrative = {
            **proposed_narrative,
            "linked_event_ids": writer_ids["event_ids"],
            "linked_scene_ids": writer_ids["scene_ids"],
            "linked_diary_ids": writer_ids["diary_ids"],
            "linked_darkroom_ids": writer_ids["darkroom_ids"],
            "linked_upload_ids": writer_ids["upload_ids"],
        }
        materials = self.materialize(writer_narrative)
        if materials.get("status") != "ok":
            return JSONResponse({**materials, "writes_performed": []}, status_code=409)
        sealed_materials = materials
        if mode == "update":
            sealed_materials = self.materialize(proposed_narrative)
            if sealed_materials.get("status") != "ok":
                return JSONResponse({**sealed_materials, "writes_performed": []}, status_code=409)
        return JSONResponse({
            "status": "ready",
            "narrative_id": narrative_id,
            "mode": mode,
            "title": str(narrative.get("title") or narrative_id),
            "writing_focus": str(narrative.get("current_status_cue") or "").removeprefix("主题：")
                if str(narrative.get("current_status_cue") or "").startswith("主题：") else "",
            "current_body": str(narrative.get("body") or ""),
            "materials": materials,
            "material_scope": selection["scope"],
            "base_revision": current_revision,
            "base_document_sha256": current_hash,
            "material_counts": materials.get("material_counts") or {},
            "current_material_ids": current_material_ids,
            "proposed_material_ids": proposed_material_ids,
            "material_delta": selection["delta"],
            "material_snapshot_sha256": material_snapshot_sha256(sealed_materials),
            "writes_performed": [],
        })

    async def api_save_narrative_roll_body(self, request):
        """Save one sealed body/material preview after fresh source and CAS validation."""
        from starlette.responses import JSONResponse

        err = None
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "invalid", "reason": "invalid_json_body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"status": "invalid", "reason": "request_body_not_object"}, status_code=400)
        narrative_id = str(body.get("narrative_id") or "").strip()
        exact_body = str(body.get("body") or "")
        preview_fingerprint = str(body.get("preview_fingerprint") or "").strip().lower()
        expected_material_hash = str(body.get("expected_material_snapshot_sha256") or "").strip().lower()
        if (
            not narrative_id
            or not MEMORY_ID_RE.fullmatch(narrative_id)
            or not exact_body.strip()
            or not re.fullmatch(r"[0-9a-f]{64}", preview_fingerprint)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_material_hash)
        ):
            return JSONResponse({"status": "invalid", "reason": "invalid_body_save_request"}, status_code=400)
        current = self.rolls.read(narrative_id)
        if current.get("status") != "ok":
            return JSONResponse({"status": "not_found", "reason": "narrative_not_found"}, status_code=404)
        try:
            expected_revision = int(body.get("expected_revision"))
        except (TypeError, ValueError):
            return JSONResponse({"status": "invalid", "reason": "invalid_expected_revision"}, status_code=400)
        expected_document_hash = str(body.get("expected_document_sha256") or "").strip().lower()
        if expected_revision != int(current.get("revision") or 0):
            return JSONResponse({"status": "conflict", "reason": "revision_mismatch"}, status_code=409)
        if expected_document_hash != str(current.get("document_sha256") or "").strip().lower():
            return JSONResponse({"status": "conflict", "reason": "document_hash_mismatch"}, status_code=409)
        try:
            proposed_material_ids = normalize_material_ids(
                body.get("proposed_material_ids"),
                fallback=material_ids_from_narrative(current),
            )
        except ValueError as exc:
            return JSONResponse({"status": "invalid", "reason": str(exc)}, status_code=400)
        proposed_narrative = {
            **current,
            "linked_event_ids": proposed_material_ids["event_ids"],
            "linked_scene_ids": proposed_material_ids["scene_ids"],
            "linked_diary_ids": proposed_material_ids["diary_ids"],
            "linked_darkroom_ids": proposed_material_ids["darkroom_ids"],
            "linked_upload_ids": proposed_material_ids["upload_ids"],
        }
        fresh_materials = self.materialize(proposed_narrative)
        if fresh_materials.get("status") != "ok":
            return JSONResponse({**fresh_materials, "writes_performed": []}, status_code=409)
        fresh_material_hash = material_snapshot_sha256(fresh_materials)
        if fresh_material_hash != expected_material_hash:
            return JSONResponse({
                "status": "conflict",
                "reason": "material_snapshot_changed",
                "current_material_snapshot_sha256": fresh_material_hash,
                "writes_performed": [],
            }, status_code=409)
        expected_fingerprint = narrative_preview_fingerprint(
            narrative_id=narrative_id,
            revision=expected_revision,
            document_sha256=expected_document_hash,
            body=exact_body,
            material_snapshot_sha256_value=fresh_material_hash,
        )
        if preview_fingerprint != expected_fingerprint:
            return JSONResponse({"status": "conflict", "reason": "preview_fingerprint_mismatch", "writes_performed": []}, status_code=409)
        result = self.rolls.save_body(
            narrative_id,
            exact_body,
            expected_revision=expected_revision,
            expected_document_sha256=expected_document_hash,
            source_scene_ids=proposed_material_ids["scene_ids"],
            source_event_ids=proposed_material_ids["event_ids"],
            source_diary_ids=proposed_material_ids["diary_ids"],
            source_darkroom_ids=proposed_material_ids["darkroom_ids"],
            source_upload_ids=proposed_material_ids["upload_ids"],
            material_snapshot=render_material_snapshot(fresh_materials),
        )
        if result.get("status") == "updated":
            current = self.rolls.read(narrative_id)
            result["absorbed_revision_proposal_ids"] = self.inbox.mark_absorbed(
                narrative_id,
                source_scene_ids=list(current.get("linked_scene_ids") or []),
                revision=int(result.get("revision") or 0),
            )
            result["bound_new_roll_proposal_ids_removed"] = (
                self.inbox.reconcile_bound_new_roll_materials(
                    bound_event_ids=set(current.get("linked_event_ids") or []),
                    bound_scene_ids=set(current.get("linked_scene_ids") or []),
                )
            )
        status_code = (
            404
            if result.get("status") == "not_found"
            else 409
            if result.get("status") == "conflict"
            else 400
            if result.get("status") == "invalid"
            else 500
            if result.get("status") == "error"
            else 200
        )
        return JSONResponse(result, status_code=status_code)

    async def api_narrative_arc_cards(self, request):
        """Return the bounded active Arc card index without narrative prose."""
        from starlette.responses import JSONResponse

        err = None
        if err:
            return err
        result = self.rolls.list(query="", limit=100)
        automatic_counts = self.events.arc_event_link_counts()
        items = [
            {
                **item,
                "automatic_linked_event_count": automatic_counts.get(
                    str(item.get("arc_key") or ""), 0
                ),
            }
            for item in result.get("items") or []
            if str(item.get("arc_key") or "")
            and str(item.get("lifecycle") or "active") == "active"
        ]
        return JSONResponse(
            {
                "status": "ok",
                "items": items,
                "count": len(items),
                "body_included": False,
                "writes_performed": [],
            }
        )

    async def api_append_narrative_arc_event_materials(self, request):
        """Append active Event receipts to one existing Arc without rewriting its prose."""
        from starlette.responses import JSONResponse

        err = None
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "invalid", "reason": "invalid_json_body"}, status_code=400)
        if not isinstance(body, dict) or set(body) != {
            "arc_key",
            "expected_revision",
            "expected_document_sha256",
            "events",
        }:
            return JSONResponse({"status": "invalid", "reason": "invalid_request_fields"}, status_code=400)
        events = body.get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= 12:
            return JSONResponse({"status": "invalid", "reason": "events_must_have_1_to_12_items"}, status_code=400)

        verified: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in events:
            if not isinstance(raw, dict) or set(raw) != {"event_id", "fingerprint"}:
                return JSONResponse({"status": "invalid", "reason": "invalid_event_receipt_fields"}, status_code=400)
            event_id = str(raw.get("event_id") or "").strip()
            fingerprint = str(raw.get("fingerprint") or "").strip().lower()
            if not re.fullmatch(r"event_[0-9a-f]{24}", event_id) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                return JSONResponse({"status": "invalid", "reason": "invalid_event_receipt"}, status_code=400)
            if event_id in seen:
                return JSONResponse({"status": "invalid", "reason": "duplicate_event_id"}, status_code=400)
            seen.add(event_id)
            event = self.events.read(event_id, include_sources=True)
            if not event:
                return JSONResponse({"status": "invalid", "reason": "event_not_found", "event_id": event_id}, status_code=400)
            if str(event.get("item_type") or "") != "event" or str(event.get("status") or "") != "active":
                return JSONResponse({"status": "invalid", "reason": "event_not_active", "event_id": event_id}, status_code=400)
            actual_fingerprint = str(event.get("fingerprint") or "").strip().lower()
            if actual_fingerprint != fingerprint:
                return JSONResponse(
                    {
                        "status": "conflict",
                        "reason": "event_fingerprint_mismatch",
                        "event_id": event_id,
                        "expected_fingerprint": fingerprint,
                        "current_fingerprint": actual_fingerprint,
                    },
                    status_code=409,
                )
            verified.append({"event_id": event_id, "fingerprint": fingerprint})

        arc_key = str(body.get("arc_key") or "").strip()
        current_arc = self.rolls.read_by_arc_key(arc_key)
        if current_arc.get("status") != "ok":
            status_code = 404 if current_arc.get("status") == "not_found" else 400
            return JSONResponse(current_arc, status_code=status_code)
        try:
            expected_revision = int(body.get("expected_revision"))
        except (TypeError, ValueError):
            return JSONResponse({"status": "invalid", "reason": "invalid_expected_revision"}, status_code=400)
        expected_hash = str(body.get("expected_document_sha256") or "").strip().lower()
        if (
            expected_revision != int(current_arc.get("revision") or 0)
            or expected_hash != str(current_arc.get("document_sha256") or "").strip().lower()
        ):
            return JSONResponse(
                {
                    "status": "conflict",
                    "reason": "arc_revision_or_document_drift",
                    "arc_key": arc_key,
                    "current_revision": int(current_arc.get("revision") or 0),
                    "current_document_sha256": str(current_arc.get("document_sha256") or ""),
                },
                status_code=409,
            )
        try:
            result = self.events.link_arc_events(arc_key, verified)
        except FactEventSettlementBlockedError as exc:
            return JSONResponse({"status": "conflict", "reason": str(exc)}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"status": "invalid", "reason": str(exc)}, status_code=400)
        result.update(
            {
                "narrative_id": current_arc.get("narrative_id"),
                "revision": current_arc.get("revision"),
                "document_sha256": current_arc.get("document_sha256"),
                "authored_body_sha256_before": current_arc.get("body_sha256"),
                "authored_body_sha256_after": current_arc.get("body_sha256"),
            }
        )
        result["verified_event_ids"] = [row["event_id"] for row in verified]
        result["writes_performed"] = (
            [{"type": "arc_event_link", "arc_key": arc_key, "event_ids": result.get("event_ids")}]
            if result.get("status") == "updated"
            else []
        )
        status_code = (
            404
            if result.get("status") == "not_found"
            else 409
            if result.get("status") == "conflict"
            else 400
            if result.get("status") in {"invalid", "error"}
            else 200
        )
        return JSONResponse(result, status_code=status_code)
