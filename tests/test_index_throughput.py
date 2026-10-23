"""Offline bounded embedding, recovery and incremental-work contracts."""
from contextlib import closing
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index
from serein.recall.index_safety import IndexBlocked,index_status,resume_index
from serein.recall.vectors import fill_vectors
from serein.recall.passages import fill_passages
from serein.recall.worker import update_pending,IndexWorker


@pytest.fixture
def indexed(tmp_path):
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True)
    with Store(settings.database) as store,store.transaction(immediate=True):
        for i in range(8):store.create(str(i),'event','Synthetic event',f'Synthetic body {i}')
    build_index(settings.database,settings.index)
    profile=dict(model='synthetic',provider_host='synthetic.invalid',document_instruction='',query_instruction='',max_chars=6000)
    with closing(sqlite3.connect(settings.index)) as conn,conn:
        conn.executemany('INSERT INTO settings VALUES (?,?)',[('embedding_profile',json.dumps(profile)),('embedding_dimension','2')])
    class Client:
        dimension=2
        def __init__(self):self.profile=profile;self.inputs=[]
        def documents(self,texts):
            self.inputs.extend(texts)
            return [[1,int(text.rsplit(' ',1)[-1])+1] for text in texts]
    return settings,Client()


def test_index_options_are_strict_opt_in_and_do_not_enable_memory(indexed):
    settings,_=indexed
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    state=client.get('/v1/settings').json()
    assert state['recall']['embedding_batch_size']==16 and state['recall']['embedding_concurrency']==1
    assert state['recall']['index_catch_up'] is False
    state=client.patch('/v1/settings',json={'recall':{'embedding_batch_size':32,'embedding_concurrency':2,'index_catch_up':True}}).json()
    assert state['recall']['embedding_concurrency']==2 and state['upstream']['memory_enabled'] is False
    for key,values in {'embedding_batch_size':[0,65,True,'32',1.5,None],
                       'embedding_concurrency':[0,5,True,'2',1.5,None],
                       'index_catch_up':[1,'true',None]}.items():
        for value in values:
            assert client.patch('/v1/settings',json={'recall':{key:value}}).status_code==422
            with pytest.raises(ValueError):save_settings(settings.database,{'recall':{key:value}})
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM index_outbox').fetchone()[0]==0


def test_parallel_results_keep_their_owners_and_default_stays_serial(indexed):
    settings,client=indexed
    original=client.documents;active=0;maximum=0;lock=threading.Lock();gate=threading.Barrier(2);parallel=False
    def documents(texts):
        nonlocal active,maximum
        with lock:active+=1;maximum=max(maximum,active)
        if parallel:gate.wait(timeout=5)
        try:return original(texts)
        finally:
            with lock:active-=1
    client.documents=documents
    assert fill_vectors(settings,client=client,batch_size=1)['requests']==8
    assert maximum==1
    with closing(sqlite3.connect(settings.index)) as conn,conn:conn.execute('DELETE FROM vectors')
    parallel=True
    save_settings(settings.database,{'recall':{'embedding_batch_size':2,'embedding_concurrency':2}})
    report=fill_vectors(settings,client=client)
    assert maximum==2 and report['requests']==4 and report['embedded']==8
    with closing(sqlite3.connect(settings.index)) as conn:
        for key,encoded in conn.execute('SELECT id,embedding FROM vectors'):
            vector=json.loads(encoded)
            assert vector[1]/vector[0]==pytest.approx(int(key)+1)
    client.inputs=[]
    assert fill_vectors(settings,client=client)['requests']==0 and client.inputs==[]


def test_failure_drains_successful_siblings_without_starting_more_calls(indexed):
    settings,client=indexed;gate=threading.Barrier(2);original=client.documents;calls=[]
    def documents(texts):
        calls.extend(texts);gate.wait(timeout=5)
        if texts[0].endswith(' 0'):raise ValueError('Synthetic invalid response')
        time.sleep(.03)
        return original(texts)
    client.documents=documents
    with pytest.raises(IndexBlocked):fill_vectors(settings,client=client,batch_size=1,concurrency=2)
    assert set(calls)=={'Synthetic body 0','Synthetic body 1'}
    assert index_status(settings.database)['attempts']==1
    with closing(sqlite3.connect(settings.index)) as conn:
        assert conn.execute('SELECT id FROM vectors').fetchall()==[('1',)]
    # The same failed budget also blocks a different worker/entry point.
    with pytest.raises(IndexBlocked):update_pending(settings,client=client)
    resume_index(settings.database,index_status(settings.database)['recovery_token'])
    client.documents=original;client.inputs=[]
    assert fill_vectors(settings,client=client,batch_size=2)['embedded']==7
    assert 'Synthetic body 1' not in client.inputs


def test_parallel_transient_failure_preserves_backoff_budget(indexed):
    from serein.adapters.embedding import EmbeddingTransientError
    settings,client=indexed;gate=threading.Barrier(2);calls=[]
    def documents(texts):
        calls.extend(texts);gate.wait(timeout=5)
        raise EmbeddingTransientError('Synthetic rate limit',retry_after=30)
    client.documents=documents
    with pytest.raises(IndexBlocked):fill_vectors(settings,client=client,batch_size=1,concurrency=2)
    state=index_status(settings.database)
    assert state['status']=='backoff' and state['attempts']==1 and len(calls)==2
    with pytest.raises(IndexBlocked):fill_vectors(settings,client=client,batch_size=1,concurrency=4)
    assert len(calls)==2 and index_status(settings.database)['attempts']==1


def test_mixed_parallel_failures_pause_instead_of_retrying_a_permanent_error(indexed):
    from serein.adapters.embedding import EmbeddingTransientError
    settings,client=indexed;gate=threading.Barrier(2)
    def documents(texts):
        gate.wait(timeout=5)
        if texts[0].endswith(' 0'):raise EmbeddingTransientError('Synthetic temporary error')
        time.sleep(.03)
        raise ValueError('Synthetic permanent contract failure')
    client.documents=documents
    with pytest.raises(IndexBlocked):fill_vectors(settings,client=client,batch_size=1,concurrency=2)
    assert index_status(settings.database)['status']=='paused'
    assert index_status(settings.database)['reason']=='index_configuration_or_contract_error'


def test_parallel_interruption_is_not_automatically_replayed(indexed):
    settings,client=indexed;gate=threading.Barrier(2);calls=[]
    def documents(texts):
        calls.extend(texts);gate.wait(timeout=5)
        if texts[0].endswith(' 0'):raise KeyboardInterrupt()
        time.sleep(.03)
        return [[1,0] for _ in texts]
    client.documents=documents
    with pytest.raises(KeyboardInterrupt):fill_vectors(settings,client=client,batch_size=1,concurrency=2)
    assert index_status(settings.database)['status']=='running'
    with pytest.raises(IndexBlocked):fill_vectors(settings,client=client)
    assert len(calls)==2 and index_status(settings.database)['status']=='paused'


def test_incremental_worker_never_reads_unrelated_bodies_for_coverage(indexed,monkeypatch):
    settings,client=indexed;read=Store.read
    from serein.core.store import promoted_scene_id
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO index_outbox(document_id) VALUES ('0')")
    def targeted(self,key,**kwargs):
        assert key in ('0',promoted_scene_id('0')),'An unrelated document was read during incremental indexing'
        return read(self,key,**kwargs)
    monkeypatch.setattr(Store,'read',targeted)
    assert update_pending(settings,client=client)['updated']==1
    assert client.inputs==['Synthetic body 0']


def test_queue_catch_up_is_opt_in_and_new_writes_survive_the_selected_window(indexed):
    settings,client=indexed
    with Store(settings.database) as store,store.transaction(immediate=True):
        for i in range(8,703):store.create(str(i),'event','Synthetic event',f'Synthetic body {i}')
        store.conn.executemany('INSERT INTO index_outbox(document_id) VALUES (?)',((str(i),) for i in range(703)))
    assert update_pending(settings,client=client)['updated']==100
    save_settings(settings.database,{'recall':{'index_catch_up':True}})
    original=client.documents;arrived=False
    def documents(texts):
        nonlocal arrived
        if not arrived:
            arrived=True
            with Store(settings.database) as store:
                store.conn.execute("INSERT INTO index_outbox(document_id) VALUES ('0')")
        return original(texts)
    client.documents=documents
    result=update_pending(settings,client=client)
    assert result['updated']==500 and result['catch_up'] and result['remaining']==104
    assert update_pending(settings,client=client)['remaining']==0


def test_catch_up_waits_after_idle_or_failure_but_not_between_backlogged_batches(indexed,monkeypatch):
    settings,_=indexed;worker=IndexWorker(settings);delays=[]
    class Stop:
        def is_set(self):return len(delays)==3
        def wait(self,seconds):delays.append(seconds)
    worker.stop=Stop();results=iter([{'catch_up':True,'remaining':20},{'status':'current'},IndexBlocked('Synthetic pause')])
    def update(*args,**kwargs):
        result=next(results)
        if isinstance(result,Exception):raise result
        return result
    monkeypatch.setattr('serein.recall.worker.update_pending',update)
    worker.run()
    assert delays==[.1,5,5]


def test_passage_fill_detects_profile_changes_before_persisting(indexed):
    settings,client=indexed
    save_settings(settings.database,{'recall':{'passages_enabled':True,'passage_min_chars':1}})
    profile=client.profile.copy()
    def documents(texts):
        with closing(sqlite3.connect(settings.index)) as conn,conn:
            conn.execute("UPDATE settings SET value=? WHERE key='embedding_profile'",(json.dumps({**profile,'model':'changed'}),))
        return [[1,0] for _ in texts]
    client.documents=documents
    with pytest.raises(IndexBlocked):fill_passages(settings,client=client,batch_size=2,concurrency=2)
    with closing(sqlite3.connect(settings.index)) as conn:
        assert conn.execute('SELECT count(*) FROM passages WHERE embedding IS NOT NULL').fetchone()[0]==0


def test_parallel_passages_keep_exact_offsets_and_reject_edited_owners(indexed):
    settings,client=indexed
    with Store(settings.database) as store,store.transaction(immediate=True):
        for i in range(8):store.revise(str(i),expected_revision=1,title='Synthetic event',body_md=f'{i} '+'Synthetic paragraph. '*30)
    save_settings(settings.database,{'recall':{'passages_enabled':True,'passage_min_chars':250,'embedding_batch_size':1,'embedding_concurrency':2}})
    gate=threading.Barrier(2)
    def documents(texts):
        if texts[0].startswith('Synthetic event\n0 '):
            with Store(settings.database) as store:
                store.revise('0',expected_revision=2,title='Changed',body_md='Changed synthetic original')
        gate.wait(timeout=5)
        return [[1,0] for _ in texts]
    client.documents=documents
    result=fill_passages(settings,client=client)
    with closing(sqlite3.connect(settings.index)) as conn,Store(settings.database) as store:
        stale=conn.execute("SELECT count(*) FROM passages WHERE document_id='0'").fetchone()[0]
        assert result['changed']==stale>0 and result['embedded']==result['planned']-stale
        assert result['coverage']['missing']==stale
        assert conn.execute("SELECT count(*) FROM passages WHERE document_id='0' AND embedding IS NOT NULL").fetchone()[0]==0
        for key,start,end,text in conn.execute('SELECT document_id,start_offset,end_offset,text FROM passages'):
            if key!='0':assert store.read(key)['body_md'][start:end]==text
