"""Short preferences with authored keywords; no semantic retrieval."""

import json
from pathlib import Path
from uuid import uuid4

from .store import Store


SUBJECTS = ("user", "ai", "shared")


def validate(value, *, deleting=False):
    if value.get("subject") not in SUBJECTS:
        raise ValueError("subject must be user, ai or shared")
    result = {"subject": value["subject"]}
    polarity = value.get("polarity")
    if polarity not in ("like", "dislike") and not (deleting and polarity is None):
        raise ValueError("polarity must be like or dislike")
    if polarity is not None:
        result["polarity"] = polarity
    for field, maximum in (("content", 500 if deleting else 50), ("reason", 1000)):
        text = value.get(field)
        if not isinstance(text, str) or not text.strip() or len(text) > maximum:
            raise ValueError(f"{field} must contain 1..{maximum} characters")
        result[field] = text.strip()
    keywords = value.get("keywords")
    if (not isinstance(keywords, list) or not 1 <= len(keywords) <= 32
            or any(not isinstance(word, str) or not word.strip() or len(word) > 80 for word in keywords)):
        raise ValueError("请填写 1 至 32 个关键词，每个最多 80 字")
    result["keywords"] = list(dict.fromkeys(word.strip() for word in keywords))
    return result


def record(row):
    return {"id": row["key"], **json.loads(row["payload_json"]),
            **{field: row[field] for field in ("revision", "created_at", "updated_at")}}


class Preferences:
    def __init__(self, database):
        self.database = database

    def _rows(self):
        if not Path(self.database).exists():
            return []
        with Store(self.database, read_only=True) as store:
            if not store.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='personal_records'").fetchone():
                return []
            return store.conn.execute(
                "SELECT * FROM personal_records WHERE scope='preference' AND deleted=0 "
                "ORDER BY updated_at DESC,key").fetchall()

    def read(self, query="", subject="", limit=50, offset=0):
        if subject and subject not in SUBJECTS:
            raise ValueError("Unknown preference subject")
        if not isinstance(query, str) or len(query) > 1000 or not 1 <= limit <= 500 or offset < 0:
            raise ValueError("Invalid preference search")
        query = query.strip().casefold()
        items = [record(row) for row in self._rows()]
        items = [item for item in items if (not subject or item["subject"] == subject)
                 and (not query or query in "\n".join(
                     (item["id"], item["content"], item["reason"], *item["keywords"])).casefold())]
        return {"items": items[offset:offset + limit], "total": len(items),
                "has_more": len(items) > offset + limit, "next_offset": offset + limit}

    def match(self, text):
        text = text.casefold()
        matches = []
        for row in self._rows():
            item = record(row)
            keywords = [word for word in item["keywords"] if word.casefold() in text]
            if keywords:
                matches.append({**item, "matched_keywords": keywords})
        return matches

    def save(self, subject, content, reason, keywords, *, polarity,
             preference_id="", expected_revision=0):
        from .personal import Personal
        row = Personal(self.database).save(
            "preference", preference_id or "pref_" + uuid4().hex,
            {"subject": subject, "polarity": polarity, "content": content,
             "reason": reason, "keywords": keywords},
            expected_revision=expected_revision)
        return {"id": row["key"], **row["value"],
                **{field: row[field] for field in ("revision", "created_at", "updated_at")}}

    def delete(self, preference_id, expected_revision):
        from .personal import Personal
        with Store(self.database, read_only=True) as store:
            row = store.conn.execute(
                "SELECT * FROM personal_records WHERE scope='preference' AND key=? AND deleted=0",
                (preference_id,)).fetchone()
        if row is None:
            raise ValueError("Preference not found")
        Personal(self.database).save(
            "preference", preference_id, json.loads(row["payload_json"]),
            expected_revision=expected_revision, deleted=True)
        return {"id": preference_id, "deleted": True}


def preference_text(items):
    lines = []
    for item in items:
        label = {"like": "喜欢", "dislike": "不喜欢"}.get(item.get("polarity"), "喜好")
        lines.extend((f"{label} · {item['subject']} · {item['id']} · revision={item['revision']}",
                      item["content"], f"原话／原因：{item['reason']}", ""))
    return "\n".join(lines).strip()


def surface(result, items):
    """Append this independent lane even when Event/Scene routing skipped recall."""
    if not items:
        return {**result, "preferences": []}
    context = "[preferences]\n" + preference_text(items) + "\n[/preferences]"
    return {**result, "preferences": items,
            "context": "\n\n".join(part for part in (context, result.get("context")) if part),
            "full_additional_context": "\n\n".join(
                part for part in (context, result.get("full_additional_context")) if part)}
