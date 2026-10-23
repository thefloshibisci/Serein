"""Model-independent exact offsets, prepared on writes/imports, never on recall."""
import json
import sqlite3
from contextlib import closing, nullcontext

from ..core.store import Store, digest, encode


def layout_path(database):
    return database.with_name(database.name + '.passages.sqlite')


def open_layouts(database):
    conn = sqlite3.connect(layout_path(database))
    conn.execute('CREATE TABLE IF NOT EXISTS layouts (document_id TEXT PRIMARY KEY, body_hash TEXT NOT NULL, min_chars INTEGER NOT NULL, spans_json TEXT NOT NULL)')
    return conn


def layout(conn, document, min_chars):
    # A settings change affects future bodies, not an already prepared layout.
    stamp = digest(encode([document['kind'], document['body_md']]))
    previous = conn.execute('SELECT body_hash,spans_json FROM layouts WHERE document_id=?', (document['id'],)).fetchone()
    if previous and previous[0] == stamp:
        return json.loads(previous[1])
    from .passages import slices
    spans = slices(document, min_chars=min_chars)
    conn.execute('INSERT OR REPLACE INTO layouts VALUES (?,?,?,?)', (document['id'], stamp, min_chars, encode(spans)))
    return spans


def prepare_layouts(settings, document_ids, *, store=None):
    from ..configured_models import recall_settings
    from .policy import RecallPolicy
    policy = RecallPolicy.from_config(recall_settings(settings))
    if not policy.passages_enabled:
        return {'status': 'disabled', 'updated': 0}
    with closing(open_layouts(settings.database)) as conn, conn, (nullcontext(store) if store else Store(settings.database,read_only=True)) as canonical:
        count = 0
        for key in set(document_ids):
            doc = canonical.read(key)
            if not doc or doc['lifecycle'] == 'deleted':
                conn.execute('DELETE FROM layouts WHERE document_id=?', (key,))
            elif doc['kind'] in ('event', 'scene'):
                layout(conn, doc, policy.passage_min_chars)
                count += 1
    return {'status': 'prepared', 'updated': count}
