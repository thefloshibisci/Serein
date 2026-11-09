"""Startup warms exact vector values without provider calls or persisted work."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from fastapi.testclient import TestClient
import pytest

from serein.api.http import create_app
from serein.config import Settings
from serein.core.store import Store
from serein.recall import typed_surface, warmup
from test_snapshot_document_efficiency import take_snapshot
from test_snapshot_vector_efficiency import PROFILE, build_runtime, create_document


@pytest.fixture
def prepared(tmp_path):
    with Store(tmp_path / 'memory.db') as store:
        for key in ('active', 'scene', 'quiet', 'archived', 'invalid'):
            create_document(store, key, kind='scene' if key == 'scene' else 'event')
    settings = build_runtime(tmp_path, {
        'active': '[1,0]', 'scene': '[0.6,0.8]', 'quiet': '[8,1]',
        'archived': '[9,1]', 'invalid': 'invalid-vector'},
        passage_rows=[('active', 0, '[0.8,0.6]', None), ('quiet', 0, '[7,1]', None),
                      ('active', 1, 'stale-passage', 'old-stamp')])
    with Store(settings.database) as store:
        store.set_manual_surface('quiet', False)
        store.set_lifecycle('archived', 'archived')
    routes = tmp_path / 'routes.json'
    routes.write_text(json.dumps({
        'format': 'serein-query-routes-v1', 'profile': PROFILE, 'dimension': 2,
        'generation': 'synthetic', 'policy': {
            'aggregation_top_k': 1, 'min_score': .7, 'min_margin': .1,
            'boundary_veto_enabled': False, 'boundary_veto_min_score': .7,
            'boundary_veto_max_deficit': .1},
        'routes': [{'name': 'synthetic', 'action': 'recall', 'threshold': None, 'vectors': [[1, 0]]}],
        'boundaries': []}), encoding='utf-8')
    typed_surface._VECTOR_CACHE.clear()
    return replace(settings, embedding={'endpoint': 'https://fixture.invalid/embeddings', 'api_key': ''},
                   reranker={'endpoint': 'https://fixture.invalid/rerank', 'model': 'synthetic', 'api_key': ''},
                   recall={'routing_file': str(routes), 'passages_enabled': True})


def test_ready_index_warms_first_snapshot_without_network_writes_or_body_reads(prepared, monkeypatch):
    from serein.adapters.embedding import EmbeddingClient
    from serein.adapters.reranker import RerankerClient
    from serein.adapters.token_window import TokenWindow

    def forbidden(*args, **kwargs):
        raise AssertionError('Warmup must not call a provider or rebuild an index')

    monkeypatch.setattr(EmbeddingClient, 'query', forbidden)
    monkeypatch.setattr(EmbeddingClient, 'documents', forbidden)
    monkeypatch.setattr(RerankerClient, '__call__', forbidden)
    monkeypatch.setattr('serein.recall.index.refresh_index', forbidden)
    connect = sqlite3.connect
    forbidden_actions = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                         sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
                         sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX,
                         sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
    reads = []
    writes = []

    def guarded_connect(path, *args, **kwargs):
        assert kwargs.get('uri') and 'mode=ro' in str(path)
        connection = connect(path, *args, **kwargs)

        def authorize(action, first, second, _database, _trigger):
            if action in forbidden_actions:
                writes.append((action, first, second))
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and first == 'revisions':
                reads.append(second)
                if second in {'body_md', 'metadata_json'}:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    before = {path: path.read_bytes() for path in (prepared.database, prepared.index)}
    with monkeypatch.context() as guards:
        guards.setattr(sqlite3, 'connect', guarded_connect)
        guards.setattr(EmbeddingClient, '__init__', forbidden)
        guards.setattr(TokenWindow, '__init__', forbidden)
        receipt = warmup.prepare(replace(prepared, embedding={**prepared.embedding,
            'tokenizer': {'path': 'synthetic-tokenizer-must-not-load', 'max_tokens': 8}}))
    assert receipt['status'] == 'warmed' and receipt['reason'] == 'existing_vectors_only'
    assert receipt['owners'] == 3 and receipt['vectors'] == 3 and receipt['invalid_vectors'] == 1
    assert writes == [] and not {'body_md', 'metadata_json'}.intersection(reads)
    assert set(typed_surface._VECTOR_CACHE) == {'[1,0]', '[0.6,0.8]', '[0.8,0.6]'}
    assert all(path.read_bytes() == contents for path, contents in before.items())
    decoded = []
    decode = typed_surface._decode_vector

    def existing_value(raw):
        if raw != 'invalid-vector':
            assert raw in typed_surface._VECTOR_CACHE
        decoded.append(raw)
        return decode(raw)

    monkeypatch.setattr(typed_surface, '_decode_vector', existing_value)
    first, _ = take_snapshot(prepared)
    assert set(first.whole) == {'active', 'scene'}
    assert decoded and 'stale-passage' not in decoded
    with Store(prepared.database) as store:
        store.set_manual_surface('active', False)
    quiet, _ = take_snapshot(prepared)
    assert set(quiet.whole) == {'scene'}


def test_existing_cue_vectors_are_read_only_and_filtered_by_model_dimension(prepared):
    cue_file = prepared.index.parent / 'memory_cue_passage_embeddings.sqlite'
    with sqlite3.connect(cue_file) as connection:
        connection.execute('CREATE TABLE memory_cue_passage_embeddings '
                           '(scene_id TEXT,embedding_model TEXT,dimension INTEGER,embedding TEXT)')
        connection.executemany('INSERT INTO memory_cue_passage_embeddings VALUES (?,?,?,?)', [
            ('scene', PROFILE['model'], 2, '[0.1,0.9]'),
            ('quiet', PROFILE['model'], 2, '[0.2,0.9]'),
            ('scene', 'different-model', 2, '[0.3,0.9]'),
            ('scene', PROFILE['model'], 3, '[0.4,0.9,0]')])
    before = cue_file.read_bytes()
    receipt = warmup.prepare(prepared)
    assert receipt['status'] == 'warmed' and receipt['vectors'] == 4
    assert '[0.1,0.9]' in typed_surface._VECTOR_CACHE
    assert not {'[0.2,0.9]', '[0.3,0.9]', '[0.4,0.9,0]'}.intersection(typed_surface._VECTOR_CACHE)
    assert cue_file.read_bytes() == before


@pytest.mark.parametrize('case', ['empty', 'missing_index', 'invalid_routes', 'missing_routes',
                                 'profile_mismatch', 'provider_mismatch', 'missing_reranker', 'foreign_index'])
def test_unprepared_instances_skip_without_creating_files(prepared, tmp_path, case):
    if case == 'empty':
        settings = Settings(prepared.database)
    elif case == 'missing_index':
        settings = replace(prepared, index=tmp_path / 'absent.sqlite')
    elif case == 'invalid_routes':
        settings = prepared
        (tmp_path / 'routes.json').write_text('{}', encoding='utf-8')
    elif case == 'missing_routes':
        settings = replace(prepared, recall={**prepared.recall,
            'routing_file': str(tmp_path / 'absent-routes.json')})
    elif case == 'provider_mismatch':
        settings = replace(prepared, embedding={**prepared.embedding,
            'endpoint': 'https://different-fixture.invalid/embeddings'})
    elif case == 'missing_reranker':
        settings = replace(prepared, reranker={})
    elif case == 'foreign_index':
        settings = prepared
        with sqlite3.connect(prepared.index) as connection:
            connection.execute('PRAGMA application_id=1')
    else:
        settings = prepared
        routes = json.loads((tmp_path / 'routes.json').read_text(encoding='utf-8'))
        routes['profile'] = {**routes['profile'], 'model': 'different-model'}
        (tmp_path / 'routes.json').write_text(json.dumps(routes), encoding='utf-8')
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob('*'))
    receipt = warmup.prepare(settings)
    assert receipt['status'] == 'skipped' and receipt['vectors'] == 0
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob('*')) == before
    assert typed_surface._VECTOR_CACHE == {}


def test_capacity_and_deadline_are_finite_safe_receipts(prepared, monkeypatch):
    monkeypatch.setattr(typed_surface, '_VECTOR_CACHE_LIMIT', 2)
    receipt = warmup.prepare(prepared)
    assert receipt['status'] == 'skipped' and receipt['reason'] == 'vector_cache_capacity'
    assert typed_surface._VECTOR_CACHE == {}
    receipt = warmup.prepare(prepared, budget_seconds=0)
    assert receipt['status'] == 'partial' and receipt['reason'] == 'time_budget'
    assert receipt['vectors'] == 0


def test_unexpected_storage_failure_is_safe_and_does_not_expose_exception(prepared, monkeypatch):
    def fail(_settings):
        raise OSError('private path and credential must remain omitted')

    monkeypatch.setattr(warmup, 'effective_settings', fail)
    receipt = warmup.prepare(prepared)
    assert receipt['status'] == 'skipped' and receipt['reason'] == 'warmup_unavailable'
    assert 'private' not in str(receipt) and 'credential' not in str(receipt)


def test_async_startup_runs_in_worker_and_waits_without_blocking_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    main_thread = threading.get_ident()
    thread_ids = []
    expected = {'status': 'warmed', 'reason': 'existing_vectors_only', 'vectors': 0, 'elapsed_ms': 0}

    def prepare(_settings):
        thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return expected

    monkeypatch.setattr(warmup, 'prepare', prepare)

    async def exercise():
        task = asyncio.create_task(warmup.startup_prepare(None))
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(.001)
        assert started.is_set() and not task.done()
        release.set()
        assert await task == expected

    asyncio.run(exercise())
    assert thread_ids and thread_ids != [main_thread]


def test_cancelled_startup_waits_for_finite_worker_before_propagating(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def prepare(_settings):
        started.set()
        assert release.wait(2)
        finished.set()
        return {}

    monkeypatch.setattr(warmup, 'prepare', prepare)

    async def exercise():
        task = asyncio.create_task(warmup.startup_prepare(None))
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(.001)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(exercise())


def test_startup_receipt_is_visible_under_uvicorn_default_logging():
    program = """
import asyncio
import logging.config
import sys
sys.path.insert(0, sys.argv[1])
from uvicorn.config import LOGGING_CONFIG
from serein.recall import warmup
logging.config.dictConfig(LOGGING_CONFIG)
warmup.prepare = lambda settings: {'status':'warmed','reason':'existing_vectors_only','vectors':3,'elapsed_ms':4}
asyncio.run(warmup.startup_prepare(None))
"""
    result = subprocess.run([sys.executable, '-c', program, str(Path(__file__).resolve().parents[1] / 'src')],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'Recall vector warmup status=warmed reason=existing_vectors_only vectors=3 elapsed_ms=4' in result.stderr


@pytest.mark.parametrize('live', [False, True])
def test_only_live_http_awaits_warmup_before_jobs_and_requests(prepared, monkeypatch, live):
    events = []
    expected = {'status': 'warmed', 'reason': 'existing_vectors_only', 'vectors': 3, 'elapsed_ms': 1}

    async def warm(_settings):
        events.append('warmup')
        await asyncio.sleep(0)
        return expected

    @asynccontextmanager
    async def lifecycle(_application):
        events.append('jobs')
        yield
        events.append('stop')

    monkeypatch.setattr(warmup, 'startup_prepare', warm)
    monkeypatch.setattr('serein.lifecycle.lifespan', lifecycle)
    app = create_app(replace(prepared, writable=live), token='synthetic', live=live)
    with TestClient(app) as client:
        assert events == (['warmup', 'jobs'] if live else ['jobs'])
        assert client.get('/health').status_code == 200
        if live:
            assert app.state.recall_warmup == expected
        else:
            assert not hasattr(app.state, 'recall_warmup')
    assert events[-1] == 'stop'
