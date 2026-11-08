import base64
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from serein.core import Conflict, Store
from serein.core.store import digest
from serein.ingest.legacy_scene import import_snapshot
from test_scene_import import artifact, snapshot


def add_deleted_history(data):
    old = deepcopy(data["evidence"][0])
    old.update(id=90, scene_id="scene_deleted")
    data["evidence"].append(old)
    data["evidence_actions"].append({"id": 91, "evidence_id": 90, "scene_id": "scene_deleted",
                                     "action": "unbind", "actor": "user", "created_at": "2026-01-03"})
    data["scene_relations"] = [{"edge_id": "edge_1", "source_scene_id": "scene_old",
                                "target_scene_id": "scene_deleted", "lifecycle_status": "archived",
                                "active": 0, "reason": "original explanation", "source_hash": "oldhash"}]
    data["scene_proposals"] = [{"proposal_id": "proposal_1", "source_scene_id": "scene_old",
                                "target_scene_id": "scene_deleted", "status": "pending"}]


def test_deleted_evidence_actions_and_relations_survive_without_resurrecting_owner(tmp_path, snapshot):
    add_deleted_history(snapshot)
    with Store(tmp_path / "test.db") as store:
        report = import_snapshot(store, snapshot)
        assert store.read("scene_deleted") is None
        assert report["detached_evidence"] == {"owner_deleted": 1}
        assert report["relations"]["scene_relations"]["deleted_endpoints"] == 1
        assert report["relations"]["scene_relations"]["missing_endpoints"] == 0
        assert report["relations"]["scene_relations"]["active_with_unavailable_endpoints"] == 0
        assert store.conn.execute("SELECT status FROM scene_proposals").fetchone()[0] == "pending"
        assert store.conn.execute("SELECT count(*) FROM detached_import_records").fetchone()[0] == 2
        repeat = import_snapshot(store, snapshot)
        assert repeat["inserted_bindings"] == 0
        assert store.conn.execute("SELECT count(*) FROM detached_import_records").fetchone()[0] == 2


def test_missing_endpoints_are_reported_without_fabricating_documents(tmp_path, snapshot):
    add_deleted_history(snapshot)
    snapshot["tombstones"] = []
    snapshot["scene_relations"][0].update(lifecycle_status="active", active=1)
    with Store(tmp_path / "test.db") as store:
        report = import_snapshot(store, snapshot)
        assert store.read("scene_deleted") is None
        assert report["detached_evidence"] == {"owner_missing": 1}
        assert report["relations"]["scene_relations"]["missing_endpoints"] == 1
        assert report["relations"]["scene_relations"]["active_with_unavailable_endpoints"] == 1


def test_relation_conflict_rolls_back_the_document_batch(tmp_path, snapshot):
    add_deleted_history(snapshot)
    with Store(tmp_path / "test.db") as store:
        store.conn.execute("INSERT INTO scene_relations VALUES (?, ?, ?, ?, ?, ?, ?)",
                           ("edge_1", "other", "a", "b", "active", 1, "{}"))
        with pytest.raises(Conflict, match="scene_relations"):
            import_snapshot(store, snapshot)
        assert store.read("scene_old") is None
        assert store.conn.execute("SELECT count(*) FROM detached_import_records").fetchone()[0] == 0
        assert store.conn.execute("SELECT count(*) FROM import_records").fetchone()[0] == 0


def test_version_one_database_upgrades_without_losing_body(tmp_path):
    path = tmp_path / "version1.db"
    schema = Path(__file__).parents[1] / "src/serein/core/schema.sql"
    with sqlite3.connect(path) as db:
        db.executescript(schema.read_text(encoding="utf-8"))
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                   ("existing", "scene", 1, "active", 0, "created", "updated"))
        db.execute("INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                   ("existing", 1, "title", "original", digest("original"), "{}", "recorded"))
    with Store(path) as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert store.read("existing")["body_md"] == "original"
        assert store.surface_state("existing")["reasons"] == ["manual_disabled"]
