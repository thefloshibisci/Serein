"""Prepare existing vector values in memory before live HTTP accepts requests.

This never prepares an index, generates embeddings or records a recall. Only
the exact-value vector decoder is shared with requests; visibility is not cached.
"""

import asyncio
from contextlib import ExitStack
import json
import logging
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlparse

from . import typed_surface
from .index import APPLICATION_ID
from ..configured_models import effective_settings


class _DeadlineReached(Exception):
    pass


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise _DeadlineReached


def _connect(stack, path, deadline):
    _check_deadline(deadline)
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=max(.001, min(1.0, deadline - time.monotonic())))
    stack.callback(connection.close)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return connection


def _batches(identifiers):
    identifiers = tuple(identifiers)
    for offset in range(0, len(identifiers), 400):
        yield identifiers[offset:offset + 400]


def _compatible_profile(selected, profile, dimension):
    """Check existing preparation metadata without constructing any model client.

    This is only a cache compatibility check. It does not claim route readiness;
    requests retain their independent complete route and policy validation.
    """
    if (type(dimension) is not int or dimension < 1 or not isinstance(profile, dict)
            or not isinstance(profile.get('model'), str) or not profile['model']):
        return False
    endpoint = urlparse(selected.embedding.get('endpoint', ''))
    local_http = endpoint.scheme == 'http' and endpoint.hostname in ('localhost', '127.0.0.1', '::1')
    if ((endpoint.scheme != 'https' and not local_http) or endpoint.username or endpoint.password
            or not endpoint.hostname or endpoint.hostname != profile.get('provider_host')):
        return False
    routes = json.loads(Path(selected.recall['routing_file']).read_text(encoding='utf-8'))
    return (isinstance(routes, dict) and routes.get('format') == 'serein-query-routes-v1'
            and routes.get('profile') == profile and routes.get('dimension') == dimension
            and bool(routes.get('generation')) and isinstance(routes.get('policy'), dict)
            and isinstance(routes.get('routes'), list) and bool(routes['routes'])
            and isinstance(routes.get('boundaries'), list))


def _vector_queries(index, cues, owners, *, passages, model, dimension):
    for batch in _batches(owners):
        markers = ','.join('?' for _ in batch)
        yield index, f'SELECT embedding FROM vectors WHERE id IN ({markers})', batch
        if passages:
            yield index, (
                'SELECT p.embedding FROM passages p JOIN documents d ON d.id=p.document_id AND d.stamp=p.stamp '
                f'WHERE p.document_id IN ({markers}) AND p.embedding IS NOT NULL'), batch
    if cues is not None:
        for batch in _batches(key for key, kind in owners.items() if kind == 'scene'):
            markers = ','.join('?' for _ in batch)
            yield cues, ('SELECT embedding FROM memory_cue_passage_embeddings '
                         f'WHERE scene_id IN ({markers}) AND embedding_model=? AND dimension=?'), (
                             *batch, model, dimension)


def prepare(settings, *, budget_seconds=30.0):
    """One read-only pass with a cooperative time and cache-capacity budget."""
    started = time.monotonic()
    deadline = started + max(0.0, min(30.0, float(budget_seconds)))
    receipt = {'status': 'skipped', 'reason': 'memory_not_prepared',
               'owners': 0, 'vectors': 0, 'invalid_vectors': 0}
    try:
        _check_deadline(deadline)
        selected = effective_settings(settings)
        if not (selected.embedding and selected.reranker and selected.index and selected.index.is_file()
                and selected.recall.get('routing_file')):
            return receipt
        _check_deadline(deadline)
        with ExitStack() as stack:
            canonical = _connect(stack, selected.database, deadline)
            index = _connect(stack, selected.index, deadline)
            if (index.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or index.execute('PRAGMA user_version').fetchone()[0] != 1
                    or not index.execute("SELECT 1 FROM settings WHERE key='build_counts'").fetchone()):
                return receipt
            indexed = {row['id'] for row in index.execute(
                "SELECT id FROM documents WHERE kind IN ('event','scene')")}
            # These headers only bound work. Request-time reads still check all
            # source access, freshness, domain rules and live surface settings.
            owners = {row['id']: row['kind'] for row in canonical.execute(
                "SELECT id,kind FROM documents WHERE kind IN ('event','scene') "
                "AND lifecycle='active' AND manual_surface=1") if row['id'] in indexed}
            receipt['owners'] = len(owners)
            profile = json.loads(index.execute(
                "SELECT value FROM settings WHERE key='embedding_profile'").fetchone()[0])
            dimension = json.loads(index.execute(
                "SELECT value FROM settings WHERE key='embedding_dimension'").fetchone()[0])
            if not _compatible_profile(selected, profile, dimension):
                return receipt
            _check_deadline(deadline)
            tables = {row[0] for row in index.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            passages = bool(selected.recall.get('passages_enabled') and 'passages' in tables)
            cue_path = Path(selected.index).parent / 'memory_cue_passage_embeddings.sqlite'
            cues = _connect(stack, cue_path, deadline) if cue_path.is_file() else None
            queries = list(_vector_queries(index, cues, owners, passages=passages,
                                          model=profile['model'], dimension=dimension))
            count = 0
            for connection, statement, parameters in queries:
                _check_deadline(deadline)
                count += connection.execute('SELECT count(*) FROM (' + statement + ')', parameters).fetchone()[0]
                if count > typed_surface._VECTOR_CACHE_LIMIT:
                    receipt['reason'] = 'vector_cache_capacity'
                    return receipt
            for connection, statement, parameters in queries:
                _check_deadline(deadline)
                for row in connection.execute(statement, parameters):
                    _check_deadline(deadline)
                    if receipt['vectors'] + receipt['invalid_vectors'] >= typed_surface._VECTOR_CACHE_LIMIT:
                        receipt.update(status='partial', reason='vector_cache_capacity')
                        return receipt
                    try:
                        typed_surface._decode_vector(row['embedding'])
                    except (TypeError, ValueError, OverflowError):
                        receipt['invalid_vectors'] += 1
                    else:
                        receipt['vectors'] += 1
            receipt.update(status='warmed', reason='existing_vectors_only')
    except _DeadlineReached:
        receipt.update(status='partial', reason='time_budget')
    except Exception:
        # Paths, model credentials, memory text and raw error messages stay out
        # of both startup logs and the in-memory public diagnostic receipt.
        if time.monotonic() >= deadline:
            receipt.update(status='partial', reason='time_budget')
        else:
            receipt.update(status='skipped', reason='warmup_unavailable')
    finally:
        receipt['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return receipt


async def startup_prepare(settings):
    # No wait_for: cancellation cannot leave an unobserved worker warming the
    # cache while normal startup proceeds. The finite pass always ends first.
    worker = asyncio.create_task(asyncio.to_thread(prepare, settings))
    try:
        receipt = await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise
    logging.getLogger('uvicorn.error.serein_recall_warmup').info('Recall vector warmup status=%s reason=%s vectors=%s elapsed_ms=%s',
        receipt['status'], receipt['reason'], receipt['vectors'], receipt['elapsed_ms'])
    return receipt
