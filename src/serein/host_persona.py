"""Evaluate new, completed live host exchanges after their archive response.

Old uploads, imports and duplicate retries never replay a historical mood.
The archived dialogue remains the only source of the evaluator's text.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from .core.store import Store, encode, now
from .deployment import read_settings, task_model

_locks = {}


def _live(row):
    meta = json.loads(row['metadata_json'])
    return (row['source'] == 'orangechat' and row['client'] == 'orangechat'
            and meta.get('origin') == 'live' and bool(row['session_id'])
            and bool(row['source_event_id']))


def _fresh(row):
    try:
        stamp = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        return -120 <= age <= 900
    except (ValueError, TypeError):
        return False


def claim_exchange(database, identifier):
    state = read_settings(database)
    if not (state['features']['persona'] or state['features']['anti_retreat']) or not task_model(database, 'persona'):
        return None
    with Store(database) as store, store.transaction(immediate=True):
        assistant = store.conn.execute('SELECT * FROM raw_events WHERE id=?', (identifier,)).fetchone()
        if (assistant is None or assistant['role'] != 'assistant' or not _live(assistant)
                or not _fresh(assistant)
                or json.loads(assistant['metadata_json']).get('generation_completed') is not True):
            return None
        key = 'host_persona:' + str(identifier)
        if store.conn.execute('SELECT 1 FROM background_state WHERE name=?', (key,)).fetchone():
            return None
        # An assistant before this user means this is another variant/reply,
        # rather than a newly completed exchange. Never invent a missing user.
        previous = store.conn.execute(
            'SELECT * FROM raw_events WHERE source=? AND session_id=? AND conversation_id=? AND id<? '
            'ORDER BY id DESC LIMIT 17',
            (assistant['source'], assistant['session_id'], assistant['conversation_id'], identifier)).fetchall()
        if not previous or previous[0]['role'] != 'user' or not _live(previous[0]) or not _fresh(previous[0]):
            return None
        user = previous[0]
        round_key = 'feature_round:' + assistant['session_id']
        saved = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (round_key,)).fetchone()
        round_id = (json.loads(saved[0]) if saved else 0) + 1
        store.conn.execute('INSERT INTO background_state VALUES (?,?) ON CONFLICT(name) DO UPDATE '
                           'SET value_json=excluded.value_json', (round_key, encode(round_id)))
        store.conn.execute('INSERT INTO background_state VALUES (?,?)',
                           (key, encode({'status': 'running', 'at': now(), 'round': round_id,
                                         'session_id': assistant['session_id']})))
        messages = [{'role': row['role'], 'content': row['text']} for row in reversed(previous[1:]) if _live(row)]
        return {'key': key, 'window_id': assistant['session_id'], 'query': user['text'],
                'message': assistant['text'], 'messages': messages, 'round_id': round_id}


async def evaluate_uploaded(database, inserted_ids):
    from .chat_features import after_reply
    # Uploads are processed in archive order and never run inside recall.
    lock_key = str(database)
    lock = _locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        for identifier in sorted(set(inserted_ids)):
            exchange = claim_exchange(database, identifier)
            if exchange is None:
                continue
            # The evaluator may record a model error and keep the prior mood.
            # This status only says that evaluation ran; its outcome belongs
            # to persona_events and persona_session_state.
            status = 'evaluated'
            try:
                result = await after_reply(database, exchange['window_id'], exchange['query'], exchange['message'],
                                           exchange['messages'], round_id=exchange['round_id'],
                                           force_persona=exchange['round_id'] == 1)
                if result is False:
                    status = 'failed'
            except Exception as error:
                status = 'failed'
                logging.getLogger(__name__).warning('Host mood evaluation failed: %s', type(error).__name__)
            with Store(database) as store, store.transaction(immediate=True):
                row = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (exchange['key'],)).fetchone()
                saved = json.loads(row[0])
                store.conn.execute('UPDATE background_state SET value_json=? WHERE name=?',
                                   (encode({**saved, 'status': status, 'finished_at': now()}), exchange['key']))
