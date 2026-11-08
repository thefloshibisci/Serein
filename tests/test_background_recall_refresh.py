"""Synthetic databases only; no deployment data or external model requests."""
import asyncio
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.feelings import Feelings
from serein.core.personal import Personal
from serein.core.plans import Plans
from serein.core.reader import Reader
from serein.core.store import Store, encode
from serein.recall.index import build_index
from serein.recall import legacy_indexes


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(tmp_path / 'synthetic.db', tmp_path / 'index.db', writable=True)
    initialize(replace(settings, index=None))
    with Store(settings.database) as store:
        store.create('event_rain', 'event', 'Rain', 'We watched the rain together.', manual_surface=True)
    related = [{'kind': 'event', 'id': 'event_rain'}]
    feeling = Feelings(settings.database).create('I felt calm.', related, author='Synthetic', can_surface=True)
    plan = Plans(settings.database).create('I will return.', actor='Synthetic', related=related)
    Personal(settings.database).annotate('event_rain', 'Synthetic note', 'Synthetic', 'assistant', 'note')
    build_index(settings.database, settings.index)
    return settings, feeling['id'], plan['id']


def test_reader_keeps_default_associations_and_lean_reads_keep_core_state(runtime):
    settings, feeling_id, plan_id = runtime
    with Reader(settings.database) as reader:
        full = reader.read('event_rain', with_evidence=False)
        lean = reader.read('event_rain', with_evidence=False, with_related=False)
        feeling = reader.read(feeling_id, with_evidence=False, with_related=False)
    assert [item['id'] for item in full['feelings']] == [feeling_id]
    assert [item['id'] for item in full['plans']] == [plan_id]
    assert [item['id'] for item in full['annotations']] == ['note']
    assert lean == {key: value for key, value in full.items()
                    if key not in {'feelings', 'plans', 'annotations', 'annotations_scope'}}
    assert feeling['feel_sources'][0]['status'] == 'available'
    assert feeling['surface_state']['can_surface']
    assert feeling['evidence_scope'] == 'subjective_feeling_not_evidence'
    with Store(settings.database) as store:
        store.revise('event_rain', expected_revision=1, title='Correction', body_md='Later correction.')
    with Reader(settings.database) as reader:
        frozen = reader.read(feeling_id, with_related=False)['feel_sources'][0]
    assert frozen['summary'] == 'We watched the rain together.'
    assert frozen['revision'] == 1 and frozen['current_revision'] == 2


@pytest.mark.parametrize('with_related', [True, False])
def test_reader_mode_does_not_bypass_locked_or_deleted_feel_sources(runtime, with_related):
    settings, feeling_id, _plan_id = runtime
    with Store(settings.database) as store:
        store.revise('event_rain', expected_revision=1, title='Private', body_md='Private source',
                     metadata={'locked': True})
    with Reader(settings.database) as reader:
        assert not reader.read('event_rain', revision=1, with_related=with_related)['readable']
        feeling = reader.read(feeling_id, with_related=with_related)
    assert feeling['readable']
    assert feeling['feel_sources'][0]['status'] == 'restricted'
    assert not feeling['surface_state']['can_surface']
    assert 'We watched the rain' not in encode(feeling) and 'Private source' not in encode(feeling)
    with Store(settings.database) as store:
        store.set_lifecycle('event_rain', 'deleted')
    with Reader(settings.database) as reader:
        feeling = reader.read(feeling_id, with_related=with_related)
    assert feeling['feel_sources'][0]['status'] == 'unavailable'
    assert not feeling['surface_state']['can_surface']


def test_refresh_never_loads_backlinks_or_annotations(runtime, monkeypatch):
    settings, _feeling_id, _plan_id = runtime
    statements = []
    original_init = Reader.__init__

    def traced_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.store.conn.set_trace_callback(statements.append)

    def unexpected(*_args, **_kwargs):
        raise AssertionError('Background indexing must not load reverse associations')

    monkeypatch.setattr(Reader, '__init__', traced_init)
    monkeypatch.setattr('serein.core.feelings.backlinks', unexpected)
    monkeypatch.setattr('serein.core.plans.backlinks', unexpected)
    report = asyncio.run(legacy_indexes.refresh(settings))
    assert report['lexical']['indexed'] == 1
    assert report['cues']['status'] == 'disabled' and report['canonical_writes'] == 0
    assert not any('personal_records' in statement for statement in statements)
    assert not any('json_each' in statement for statement in statements)


@pytest.mark.parametrize('stage', ['read', 'lexical'])
def test_slow_scan_and_lexical_sync_leave_event_loop_responsive(runtime, monkeypatch, stage):
    settings, _feeling_id, _plan_id = runtime
    release = threading.Event()
    worker_threads = []

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        main_thread = threading.get_ident()
        original = Reader.read if stage == 'read' else legacy_indexes.FactEventLexicalShadowIndex.sync

        def slow(*args, **kwargs):
            if not worker_threads:
                worker_threads.append(threading.get_ident())
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError('Synchronous scan blocked the event loop')
            return original(*args, **kwargs)

        if stage == 'read':
            monkeypatch.setattr(Reader, 'read', slow)
        else:
            monkeypatch.setattr(legacy_indexes.FactEventLexicalShadowIndex, 'sync', slow)
        task = asyncio.create_task(legacy_indexes.refresh(settings))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not task.done()
            assert len(worker_threads) == 1 and worker_threads[0] != main_thread
            # Another request can run while the synchronous scan is still waiting.
            await asyncio.sleep(0)
        finally:
            release.set()
            report = await task
        assert report['lexical']['indexed'] == 1

    asyncio.run(run())


def test_cancelled_scan_finishes_local_stage_without_starting_cue_models(runtime, monkeypatch):
    settings, _feeling_id, _plan_id = runtime
    release = threading.Event()
    finished = threading.Event()
    provider_calls = []

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def prepare(*_args, **_kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError('Cancellation did not leave the loop responsive')
            finished.set()
            return True, [], {}, {'status': 'ok'}

        monkeypatch.setattr(legacy_indexes, '_prepare_refresh', prepare)
        monkeypatch.setattr(legacy_indexes, 'cue_index', lambda *_a, **_kw: provider_calls.append(True))
        task = asyncio.create_task(legacy_indexes.refresh(settings))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set() and not provider_calls

    asyncio.run(run())


def test_concurrent_refreshes_keep_sqlite_connections_in_their_own_threads(runtime, monkeypatch):
    settings, _feeling_id, _plan_id = runtime
    rendezvous = threading.Barrier(2, timeout=5)
    threads = set()
    lock = threading.Lock()
    original = Reader.read

    def simultaneous_read(self, *args, **kwargs):
        ident = threading.get_ident()
        with lock:
            first = ident not in threads
            threads.add(ident)
        if first:
            rendezvous.wait()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Reader, 'read', simultaneous_read)

    async def run():
        return await asyncio.gather(legacy_indexes.refresh(settings), legacy_indexes.refresh(settings))

    results = asyncio.run(run())
    assert len(threads) == 2
    assert all(result['canonical_writes'] == 0 and result['lexical']['status'] == 'ok' for result in results)
    report = legacy_indexes.lexical_index(settings).sync([
        {'item_id': 'event_rain', 'item_type': 'event', 'title': 'Rain',
         'body': 'We watched the rain together.', 'status': 'active'}], dry_run=True)
    assert report['eligible'] == 1 and report['to_index'] == 0 and report['stale_rows'] == 0


def test_cancelled_cue_request_is_closed_and_not_reissued_by_refresh(runtime, monkeypatch):
    from serein.recall.germany.memory_recall.cue_passage_shadow import CuePassageShadowIndex

    settings, _feeling_id, _plan_id = runtime
    scenes = [{'id': 'scene_synthetic', 'title': 'Window', 'cues': ['window']}]
    passages = {('scene', 'scene_synthetic'): [
        {'ordinal': 0, 'start_offset': 0, 'end_offset': 6, 'text': 'window'}]}
    calls = []
    closed = []

    class Embedding:
        enabled = True
        model = 'synthetic'
        document_instruction = ''

        async def embed_document(self, _text):
            raise AssertionError('An interrupted binder must never reach embedding')

    async def run():
        started = asyncio.Event()

        class Binder:
            model = 'synthetic'

            def __init__(self):
                self.client = self

            async def bind(self, **_materials):
                calls.append(True)
                started.set()
                await asyncio.Event().wait()

            async def close(self):
                closed.append(True)

        def cue_index(_settings, **_kwargs):
            return CuePassageShadowIndex({'state_dir': str(settings.database.parent)},
                                        Embedding(), binder=Binder())

        monkeypatch.setattr(legacy_indexes, '_prepare_refresh',
                            lambda *_a, **_kw: (True, scenes, passages, {'status': 'ok'}))
        monkeypatch.setattr(legacy_indexes, 'cue_index', cue_index)
        task = asyncio.create_task(legacy_indexes.refresh(settings))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [True] and closed == [True]
        result = await legacy_indexes.refresh(settings)
        assert result['cues']['paused_scenes'] == ['scene_synthetic:interrupted']
        assert calls == [True] and closed == [True, True]

    asyncio.run(run())


@pytest.mark.parametrize('dry_run,retry_failed', [(False, False), (True, False), (False, True)])
def test_refresh_preserves_cue_options_and_closes_client(runtime, monkeypatch, dry_run, retry_failed):
    settings, _feeling_id, _plan_id = runtime
    settings = replace(settings, recall={'passages_enabled': True})
    calls = []
    closed = []

    class Client:
        async def close(self):
            closed.append(True)

    class Cues:
        binder = SimpleNamespace(client=Client())

        async def sync(self, **kwargs):
            calls.append(kwargs)
            return {'status': 'synthetic'}

    writable = []

    def make_cues(_settings, **kwargs):
        writable.append(kwargs['writable'])
        return Cues()

    monkeypatch.setattr(legacy_indexes, 'cue_index', make_cues)
    result = asyncio.run(legacy_indexes.refresh(settings, dry_run=dry_run, retry_failed=retry_failed))
    assert writable == [not dry_run]
    assert calls[0]['dry_run'] is dry_run and calls[0]['retry_failed'] is retry_failed
    assert closed == [True] and result['cues']['status'] == 'synthetic'
