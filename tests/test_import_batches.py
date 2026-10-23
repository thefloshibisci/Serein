"""Synthetic import recovery, bounded work and opt-in throughput contracts."""
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store, encode
from serein.imports import advance_import, initialize_imports, list_imports, parse_file, stage
from serein.work_tasks import enqueue, execute, pause, status, work


@pytest.fixture
def settings(tmp_path):
    value=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True)
    initialize(value)
    return value


def content(count):
    return json.dumps({'id':'synthetic','messages':[{'id':f'm{i}',
        'role':'user' if i%2==0 else 'assistant','content':f'Synthetic original {i}',
        'timestamp':1700000000+i} for i in range(count)]})


def upload(settings,count=60):
    return stage(settings.database,content(count),'synthetic.json','conversation',False)


def test_preview_stores_ordered_input_and_reports_without_decoding_the_full_file(settings,monkeypatch):
    task=upload(settings,1000)
    with Store(settings.database) as store:
        row=store.conn.execute('SELECT * FROM file_imports').fetchone()
        assert row['total']==1000 and len(row['payload_json'])<3000
        assert 'entries' not in json.loads(row['payload_json'])
        assert store.conn.execute('SELECT count(*) FROM file_import_entries').fetchone()[0]==1000
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==0
    original=json.loads
    def bounded_loads(value,*args,**kwargs):
        assert len(value)<3000, 'An entire upload was decoded during a batch/report'
        return original(value,*args,**kwargs)
    monkeypatch.setattr(json,'loads',bounded_loads)
    result=advance_import(settings,task['id'])
    assert result['processed']==25 and result['batch_processed']==25
    assert list_imports(settings.database)[0]['preview']==task['preview']


def test_legacy_partial_upload_migrates_once_with_progress_errors_and_choices(settings):
    text=content(30);task=upload(settings,30)
    advance_import(settings,task['id'])
    data=parse_file(text,'synthetic.json',{'user_name':'User','ai_name':'AI'})
    data['sillytavern_header']={'synthetic':'retained metadata'}
    with Store(settings.database) as store:
        store.conn.execute('DELETE FROM file_import_entries')
        store.conn.execute('UPDATE file_imports SET payload_json=?,total=NULL,errors_json=?',
                           (encode(data),encode([{'entry':1,'message':'previous error'}])))
        store.conn.execute('INSERT INTO background_state VALUES (?,?)',('import-choice:'+task['id'],encode({'choice':'skip'})))
    initialize_imports(settings.database)
    initialize_imports(settings.database)
    saved=list_imports(settings.database)[0]
    assert saved['processed']==25 and saved['inserted']==25 and saved['failed']==1
    assert advance_import(settings,task['id'])['inserted']==30
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM file_import_entries').fetchone()[0]==30
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==30
        assert store.conn.execute('SELECT released FROM pipeline_import_boundaries').fetchone()[0]==0
        assert json.loads(store.conn.execute('SELECT payload_json FROM file_imports').fetchone()[0])['sillytavern_header']==data['sillytavern_header']
        assert store.conn.execute('SELECT value_json FROM background_state WHERE name=?',('import-choice:'+task['id'],)).fetchone()


def test_failure_saving_cursor_rolls_back_originals_fts_and_history_boundary(settings):
    task=upload(settings,30)
    with Store(settings.database) as store:
        store.conn.executescript("CREATE TRIGGER fail_import BEFORE UPDATE OF cursor ON file_imports "
            "WHEN NEW.cursor>OLD.cursor BEGIN SELECT RAISE(ABORT,'synthetic interruption'); END;")
    with pytest.raises(sqlite3.IntegrityError,match='synthetic interruption'):
        advance_import(settings,task['id'])
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT cursor FROM file_imports').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_events_fts').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM pipeline_import_boundaries').fetchone()[0]==0
        store.conn.execute('DROP TRIGGER fail_import')
    assert advance_import(settings,task['id'])['inserted']==25
    assert advance_import(settings,task['id'])['inserted']==30


def test_same_batch_duplicates_and_conflicts_keep_first_original_and_number(settings):
    data=json.loads(content(2));data['messages']=[data['messages'][0],data['messages'][0],
        {**data['messages'][0],'content':'Conflicting synthetic original'},data['messages'][1]]
    task=stage(settings.database,json.dumps(data),'same-id.json','conversation',False)
    result=advance_import(settings,task['id'])
    assert (result['inserted'],result['duplicate'],result['failed'])==(2,1,1)
    assert result['errors'][0]['entry']==3
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT text FROM raw_events ORDER BY id LIMIT 1').fetchone()[0]=='Synthetic original 0'
        assert store.conn.execute("SELECT count(*) FROM raw_events_fts WHERE raw_events_fts MATCH 'Synthetic'").fetchone()[0]==2
        assert store.conn.execute('SELECT count(*) FROM index_outbox').fetchone()[0]==0


def test_bad_scene_rolls_back_its_sources_bindings_and_tag_jobs_only(settings,monkeypatch):
    data={'exportDate':1700000000,'memories':[{'uuid':f's{i}','content':f'Synthetic scene {i}'} for i in range(3)]}
    task=stage(settings.database,json.dumps(data),'scenes.json','operit',True)
    original=Store.bind
    def bind(self,document_id,*args,**kwargs):
        result=original(self,document_id,*args,**kwargs)
        if self.read(document_id)['body_md']=='Synthetic scene 1':raise ValueError('Synthetic failed binding')
        return result
    monkeypatch.setattr(Store,'bind',bind)
    result=advance_import(settings,task['id'])
    assert (result['inserted'],result['failed'],result['processed'])==(2,1,3)
    assert result['errors'][0]['entry']==2
    with Store(settings.database) as store:
        for table in ('documents','sources','evidence_bindings','scene_evidence_ids','import_tag_jobs','tagging_outbox'):
            assert store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==2


def test_failed_scene_batch_rolls_back_evidence_index_and_tagging_with_cursor(settings):
    data={'exportDate':1700000000,'memories':[{'uuid':'scene','content':'Synthetic original scene'}]}
    task=stage(settings.database,json.dumps(data),'scene.json','operit',True)
    with Store(settings.database) as store:
        store.conn.executescript("CREATE TRIGGER fail_scene_progress BEFORE UPDATE OF cursor ON file_imports "
            "WHEN NEW.cursor>OLD.cursor BEGIN SELECT RAISE(ABORT,'synthetic interruption'); END;")
    with pytest.raises(sqlite3.IntegrityError,match='synthetic interruption'):
        advance_import(settings,task['id'])
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT cursor FROM file_imports').fetchone()[0]==0
        for table in ('documents','sources','evidence_bindings','scene_evidence_ids','index_outbox','import_tag_jobs','tagging_outbox'):
            assert store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0
        store.conn.execute('DROP TRIGGER fail_scene_progress')
    assert advance_import(settings,task['id'])['inserted']==1


def test_character_budget_keeps_whole_entries_and_resumes_in_order(settings,monkeypatch):
    import serein.imports as imports
    task=upload(settings,4)
    monkeypatch.setattr(imports,'MAX_BATCH_CHARS',1)
    for expected in range(1,5):
        result=advance_import(settings,task['id'],batch_size=250)
        assert result['batch_processed']==1 and result['processed']==expected
    with Store(settings.database) as store:
        assert [row[0] for row in store.conn.execute('SELECT text FROM raw_events ORDER BY id')]==[f'Synthetic original {i}' for i in range(4)]


def test_missing_staged_entry_cannot_skip_an_original_or_advance_cursor(settings):
    task=upload(settings,4)
    with Store(settings.database) as store:
        store.conn.execute('DELETE FROM file_import_entries WHERE position=1')
    with pytest.raises(ValueError,match='条目缺失'):
        advance_import(settings,task['id'])
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT cursor FROM file_imports').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0]==0


def test_concurrent_batches_serialize_cursor_and_do_not_double_count(settings):
    task=upload(settings,40)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:advance_import(settings,task['id']),range(2)))
    assert sorted(result['processed'] for result in results)==[25,40]
    result=advance_import(settings,task['id'])
    assert result['inserted']==40 and result['duplicate']==0 and result['batch_processed']==0


def test_one_batch_uses_constant_connections_and_commits(settings,monkeypatch):
    task=upload(settings,250);connections=[];commits=[];lookups=[];original=sqlite3.connect
    def connect(*args,**kwargs):
        conn=original(*args,**kwargs);connections.append(conn)
        def trace(sql):
            if sql=='COMMIT':commits.append(sql)
            if sql.startswith('SELECT source,source_event_id,text,role FROM raw_events'):lookups.append(sql)
        conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(sqlite3,'connect',connect)
    assert advance_import(settings,task['id'],batch_size=250)['inserted']==250
    assert len(connections)<=4 and len(commits)<=3
    assert len(lookups)==1
    conn=original(settings.database)
    try:
        plan=conn.execute('EXPLAIN QUERY PLAN '+lookups[0]).fetchall()
        assert any('idx_raw_events_source_event_id' in row[-1] for row in plan),plan
    finally:conn.close()


def test_large_batch_is_explicit_and_pause_then_resume_preserves_counts(settings,monkeypatch):
    import serein.imports as imports
    task=upload(settings,600);original=imports.advance_import;batch_sizes=[]
    def advance(*args,**kwargs):
        batch_sizes.append(kwargs['batch_size']);result=original(*args,**kwargs)
        pause(settings.database,'import:'+task['id'])
        return result
    monkeypatch.setattr(imports,'advance_import',advance)
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    route='/v1/imports/'+task['id']+'/continue'
    first=client.post(route,json={}).json()
    assert first['arguments']=={'large_batch':False}
    asyncio.run(execute(settings.database,first['id'],lambda:work(settings,first['id'],first['arguments']),queued_id=first['run_id']))
    assert status(settings.database,first['id'])['completed']==25
    second=client.post(route,json={'large_batch':True}).json()
    assert second['eta_seconds'] is None
    asyncio.run(execute(settings.database,second['id'],lambda:work(settings,second['id'],second['arguments']),queued_id=second['run_id']))
    assert batch_sizes==[25,250]
    assert status(settings.database,first['id'])['completed']==275
    assert list_imports(settings.database)[0]['inserted']==275


def test_eta_waits_for_samples_and_resume_clears_previous_measurements(settings,monkeypatch):
    import serein.imports as imports
    task=upload(settings,105);key='import:'+task['id'];original=imports.advance_import;measurements=[]
    def advance(*args,**kwargs):
        if measurements:measurements.append(status(settings.database,key))
        else:measurements.append({})
        result=original(*args,**kwargs)
        if result['processed']==100:pause(settings.database,key)
        return result
    monkeypatch.setattr(imports,'advance_import',advance)
    queued=enqueue(settings.database,key)
    asyncio.run(execute(settings.database,key,lambda:work(settings,key,{}),queued_id=queued['run_id']))
    assert measurements[1]['eta_seconds'] is None and measurements[2]['eta_seconds'] is None
    assert measurements[3]['eta_seconds']>0 and measurements[3]['sampled_batches']==3
    queued=enqueue(settings.database,key)
    assert queued['eta_seconds'] is None and queued['entries_per_second'] is None
