"""Synthetic preference storage, tools and keyword-only surfacing."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serein.api.gateway import routes as hook_routes
from serein.api.mcp import create_server
from serein.api.personal import routes as personal_routes
from serein.api.read_text import recall_text
from serein.application import Application
from serein.config import Settings
from serein.core.preferences import Preferences, surface
from serein.core.store import Store, encode, now
from serein.recall.service import Recall


@pytest.fixture
def fixture(tmp_path):
    settings = Settings(tmp_path / "synthetic.db", writable=True)
    with Store(settings.database):
        pass
    application = Application(settings)
    app = FastAPI()
    app.include_router(personal_routes(settings, []))
    app.include_router(hook_routes(application.services, []))
    return settings, application, Preferences(settings.database), TestClient(app)


def tool_text(server, name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[0].text if not isinstance(result, tuple) else result[0][0].text


def test_literal_match_uses_only_keywords_and_reads_do_not_write(fixture):
    settings, _, preferences, _ = fixture
    item = preferences.save("user", "奶茶喜欢三分糖", "这杯太甜了，下次三分糖。",
                            ["奶茶", "三分糖", "甜度", "奶茶"], polarity="like")
    english = preferences.save("ai", "示例：不喜欢某首音乐", "合成测试", ["Music"], polarity="dislike")
    before = settings.database.read_bytes()
    assert item["keywords"] == ["奶茶", "三分糖", "甜度"]
    assert preferences.match("想买奶茶")[0]["id"] == item["id"]
    assert preferences.match("甜度怎么选")[0]["matched_keywords"] == ["甜度"]
    assert preferences.match("这杯太甜了") == []
    assert preferences.match("music")[0]["id"] == english["id"]
    assert preferences.read("太甜")["items"][0]["id"] == item["id"]
    assert preferences.read(subject="ai")["items"][0]["id"] == english["id"]
    assert settings.database.read_bytes() == before


def test_ui_and_mcp_share_records_revisions_and_schema(fixture):
    _, application, preferences, client = fixture
    server = create_server(application)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["write_preference"].inputSchema
    assert schema["properties"]["subject"]["enum"] == ["user", "ai", "shared"]
    assert schema["properties"]["polarity"]["enum"] == ["like", "dislike"]
    assert schema["properties"]["content"]["maxLength"] == 50
    text = tool_text(server, "write_preference", subject="user", polarity="like",
                     content="奶茶喜欢三分糖", reason="合成原话", keywords=["奶茶"])
    assert "奶茶喜欢三分糖\n原话／原因：合成原话" in text
    item = preferences.read()["items"][0]
    listed = client.get("/api/personal?scope=preference").json()["items"]
    assert listed[0]["key"] == item["id"]
    edited = client.post("/api/personal", json={"scope": "preference", "key": item["id"],
        "expected_revision": item["revision"], "value": {"subject": "user", "polarity": "dislike",
        "content": "不喜欢加糖奶茶", "reason": "现在改喝无糖了", "keywords": ["奶茶", "无糖"]}})
    assert edited.status_code == 200
    assert "不喜欢加糖奶茶" in tool_text(server, "read_preferences", query=item["id"])
    current = preferences.read(item["id"])["items"][0]
    assert "Deleted preference" in tool_text(server, "delete_preference",
        preference_id=item["id"], expected_revision=current["revision"])
    assert preferences.read()["items"] == []


def test_preferences_surface_when_memory_route_skips_and_hook_disables_cards(fixture, monkeypatch):
    _, application, preferences, client = fixture
    item = preferences.save("user", "奶茶喜欢三分糖", "合成原话", ["奶茶"], polarity="like")
    monkeypatch.setattr(Recall, "run", lambda *a, **kw: {"status": "skipped", "reason": "present_chitchat",
        "cards": [], "selected_refs": [], "context": "", "full_additional_context": ""})
    result = application.services.recall("想喝奶茶", method="lexical")
    assert result["status"] == "skipped" and result["preferences"][0]["id"] == item["id"]
    assert "[preferences]" in result["context"]
    hook = client.post("/api/hook/recall", json={"query": "想喝奶茶", "max_notes": 0}).json()
    assert hook["preferences"][0]["id"] == item["id"]
    assert "奶茶喜欢三分糖" in hook["additional_context"]
    assert application.services.recall("下雨了", method="lexical")["preferences"] == []


@pytest.mark.parametrize("changes", [{"keywords": []}, {"subject": "other"}, {"reason": ""},
    {"content": "字" * 51}, {"polarity": "maybe"}, {"polarity": None}])
def test_invalid_writes_leave_no_records(fixture, changes):
    _, _, preferences, _ = fixture
    value = {"subject": "user", "polarity": "like", "content": "合成喜好",
             "reason": "合成依据", "keywords": ["关键词"]}
    with pytest.raises(ValueError):
        preferences.save(**{**value, **changes})
    assert preferences.read()["items"] == []


def test_legacy_notes_remain_readable_and_deletable(fixture):
    settings, _, preferences, client = fixture
    value = {"subject": "user", "content": "旧" * 120, "reason": "旧依据", "keywords": ["旧词"]}
    with Store(settings.database) as store, store.transaction():
        store.conn.execute("INSERT INTO personal_records VALUES (?,?,NULL,?,1,0,?,?)",
                           ("preference", "old-note", encode(value), now(), now()))
    assert preferences.read()["items"][0]["content"] == value["content"]
    response = client.post("/api/personal", json={"scope": "preference", "key": "old-note",
        "value": value, "expected_revision": 1, "deleted": True})
    assert response.status_code == 200
    assert preferences.read()["items"] == []


@pytest.mark.parametrize("status,guide", [("use_narrative_reader", "find_arc(query=...)"),
    ("use_evidence_reader", "read_memory(identifier=..., with_evidence=true)")])
def test_preference_context_keeps_next_step_guidance(status, guide):
    result = surface({"status": status, "context": "", "pools": {}}, [{"id": "pref_test",
        "subject": "user", "polarity": "like", "content": "奶茶三分糖", "reason": "合成原话",
        "keywords": ["奶茶"], "revision": 1, "created_at": "", "updated_at": ""}])
    text = recall_text(result, with_evidence=False)
    assert "奶茶三分糖" in text and guide in text
