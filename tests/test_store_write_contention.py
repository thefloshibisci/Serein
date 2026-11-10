"""A waiting writer must not read a snapshot it cannot later upgrade."""
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from serein.core.store import Store, Conflict


@pytest.mark.parametrize('action',['create','revise','lifecycle','bind'])
def test_document_mutations_wait_for_other_writer(tmp_path,action):
    db=tmp_path/'memory.db'
    with Store(db) as initial:
        initial.create('existing','scene','Before','Original')
        source=initial.add_source('source','Original evidence')
    started=threading.Event()
    def mutate():
        with Store(db) as store:
            started.set()
            if action=='create':return store.create('new','scene','New','Body')
            if action=='revise':return store.revise('existing',expected_revision=1,title='After',body_md='Revised')
            if action=='lifecycle':return store.set_lifecycle('existing','archived')
            return store.bind('existing',source)
    with Store(db) as other, ThreadPoolExecutor(max_workers=1) as pool:
        other.conn.execute('BEGIN IMMEDIATE')
        future=pool.submit(mutate)
        assert started.wait(3)
        try:
            # The old deferred read-to-write upgrade fails immediately here.
            from concurrent.futures import TimeoutError
            with pytest.raises(TimeoutError):future.result(timeout=.15)
        finally:
            other.conn.commit()
        future.result(timeout=3)
    with Store(db) as check:
        assert check.read('existing')
        if action=='create':assert check.read('new')['body_md']=='Body'
        if action=='revise':assert check.read('existing')['revision']==2
        if action=='lifecycle':assert check.read('existing')['lifecycle']=='archived'
        if action=='bind':assert check.conn.execute('SELECT count(*) FROM evidence_bindings').fetchone()[0]==1


def test_nested_document_write_rolls_back_with_outer_transaction(tmp_path):
    with Store(tmp_path/'memory.db') as store:
        with pytest.raises(RuntimeError):
            with store.transaction(immediate=True):
                store.create('new','scene','New','Body')
                raise RuntimeError('cancel entire operation')
        assert store.read('new') is None
