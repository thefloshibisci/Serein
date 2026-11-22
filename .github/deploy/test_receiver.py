"""Synthetic deployment boundary checks; no SSH, Docker or private data."""
import copy
import importlib.util
import io
import hashlib
import json
import queue
import shutil
import sqlite3
from pathlib import Path
import sys
import tarfile
import types
import urllib.error

import pytest

if sys.platform == 'win32':
    sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))
spec = importlib.util.spec_from_file_location('deployment_receiver', Path(__file__).with_name('receiver.py'))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
COMMIT = 'a' * 40


def header(**changes):
    return dict(action='deploy_url', commit=COMMIT, bytes=12, sha256='a'*64,
                url='https://example.blob.core.windows.net/test', artifact_bytes=20, **changes)


@pytest.mark.parametrize('field,value', [
    ('url','http://example.blob.core.windows.net/a'), ('url','https://evil.test/a'),
    ('url','https://example.blob.core.windows.net.evil.test/a'),
    ('url','https://user@example.blob.core.windows.net/a'),
    ('url','https://example.blob.core.windows.net:8443/a'),
    ('commit','main'), ('bytes',True), ('artifact_bytes',0), ('extra','value'),
])
def test_untrusted_headers_rejected(field,value):
    item=header();item[field]=value
    with pytest.raises((ValueError,TypeError)):
        r.metadata(io.BytesIO((json.dumps(item)+'\n').encode()))


def test_valid_header_and_truncated_stream(tmp_path):
    item=header()
    assert r.metadata(io.BytesIO((json.dumps(item)+'\n').encode()))==item
    with pytest.raises(ValueError,match='truncated_archive'):
        r.receive(io.BytesIO(b'short'),tmp_path/'archive',item)


def bundle(path, tags=None, revision=COMMIT):
    with tarfile.open(path,'w:gz') as tar:
        rows=[]
        for i,name in enumerate(r.SERVICES):
            filename=f'{i}.json'
            rows.append({'Config':filename,'RepoTags':[(tags or {}).get(name,'nanzhi/'+name+':github-'+COMMIT)]})
            raw=json.dumps({'os':'linux','architecture':'amd64','config':{'Labels':{'org.opencontainers.image.revision':revision}}}).encode()
            info=tarfile.TarInfo(filename);info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
        raw=json.dumps(rows).encode();info=tarfile.TarInfo('manifest.json');info.size=len(raw);tar.addfile(info,io.BytesIO(raw))


def test_bundle_requires_exact_service_pair_and_revision(tmp_path):
    path=tmp_path/'image.tar.gz';bundle(path)
    assert set(r.validate_bundle(path,COMMIT))==set(r.SERVICES)
    bundle(path,tags={'serein':'nanzhi/ombre-brain:github-'+COMMIT})
    with pytest.raises(ValueError,match='manifest'):r.validate_bundle(path,COMMIT)
    bundle(path,revision='b'*40)
    with pytest.raises(ValueError,match='revision'):r.validate_bundle(path,COMMIT)


@pytest.fixture
def deployment(tmp_path,monkeypatch):
    production=tmp_path/'production';production.mkdir()
    path=production/'compose.json';gate=tmp_path/'gate'
    doc={'services':{name:{'image':'old-'+name,'ports':['8000:8000'], 'networks':{'production':{'aliases':['prod']}},
         'environment':{'SEREIN_MEMORY_URL':'http://production:8011'},
         'volumes':[{'type':'bind','source':str(production/'data'/name),'target':'/data','read_only':False}]}
         for name in r.SERVICES}}
    doc['services']['unrelated']={'image':'unchanged'}
    path.write_text(json.dumps(doc))
    monkeypatch.setattr(r,'PRODUCTION',production);monkeypatch.setattr(r,'COMPOSE',path);monkeypatch.setattr(r,'MAINTENANCE',gate)
    monkeypatch.setattr(r,'latest_commit',lambda _:COMMIT)
    monkeypatch.setattr(r,'inspect_containers',lambda _: {'unrelated':{'Id':'same'}})
    monkeypatch.setattr(r,'wait_ready',lambda _:None)
    images={name:'nanzhi/'+name+':github-'+COMMIT for name in r.SERVICES}
    job=tmp_path/'job';job.mkdir()
    return doc,images,job,path,gate


def test_rehearsal_isolates_data_network_and_jobs(deployment):
    doc,images,job,_,_=deployment;before=copy.deepcopy(doc)
    test=r.rehearsal_document(doc,images,job)
    assert test['networks']=={'isolated':{'internal':True}}
    assert set(test['services'])==set(r.SERVICES)
    for svc in test['services'].values():
        assert svc['ports']==[] and svc['networks']==['isolated']
        assert Path(svc['volumes'][0]['source']).is_relative_to(job)
    assert 'lifespan=no_jobs' in test['services']['serein-memory']['command'][0]
    assert test['services']['serein']['environment']['SEREIN_MEMORY_URL']=='http://serein-memory:8011'
    assert doc==before


def test_stale_build_never_stops_production(deployment,monkeypatch):
    doc,images,job,path,gate=deployment;before=path.read_bytes();phases=[]
    monkeypatch.setattr(r,'latest_commit',lambda _:'b'*40)
    monkeypatch.setattr(r,'call',lambda *_a,**_k:pytest.fail('must not invoke docker'))
    r.promote(doc,images,job,phases.append)
    assert phases==['skipped_main_advanced'] and path.read_bytes()==before and not gate.exists()


def test_success_backs_up_before_replacing_images(deployment,monkeypatch):
    doc,images,job,path,gate=deployment;phases=[];calls=[]
    def call(args,**kwargs):
        calls.append(args)
        if args[0]=='tar':
            assert gate.exists() and json.loads(path.read_text())==doc
        return b''
    monkeypatch.setattr(r,'call',call)
    r.promote(doc,images,job,phases.append)
    final=json.loads(path.read_text())
    assert final['services']['unrelated']==doc['services']['unrelated']
    for name in r.SERVICES:
        assert final['services'][name]==dict(doc['services'][name],image=images[name])
    assert not gate.exists() and phases[-1]=='deployed'
    assert next(i for i,c in enumerate(calls) if c[:2]==['tar','-czf']) < next(i for i,c in enumerate(calls) if 'up' in c)
    assert json.loads((job/'compose-before.json').read_text())==doc


def test_failed_start_keeps_gate_and_backup_config(deployment,monkeypatch):
    doc,images,job,path,gate=deployment
    monkeypatch.setattr(r,'call',lambda *_a,**_k:b'')
    def fail(_):raise RuntimeError('synthetic_health_failure')
    monkeypatch.setattr(r,'wait_ready',fail)
    with pytest.raises(RuntimeError,match='synthetic_health_failure'):
        r.promote(doc,images,job,lambda _:None)
    assert gate.exists() and (job/'compose-before.json').exists()
    # A failed startup never silently puts the old binary on a migrated DB.
    assert json.loads(path.read_text())['services']['serein-memory']['image']==images['serein-memory']
    with pytest.raises(RuntimeError,match='recovery'):
        r.promote(doc,images,job,lambda _:None)


def test_external_writable_mount_is_refused(deployment):
    doc,*_=deployment
    doc['services']['serein']['volumes'][0]['source']='/unexpected'
    with pytest.raises(ValueError,match='mount'):r.writable_sources(doc)


@pytest.mark.parametrize('head', [COMMIT, 'b'*40])
def test_cutover_confirmation_is_bound_to_nonce_and_commit(monkeypatch,capsys,head):
    monkeypatch.setattr(r.os,'urandom',lambda _:b'1'*16)
    confirmations=queue.Queue();monkeypatch.setattr(r,'CONFIRMATIONS',confirmations)
    item={'action':'main_confirmation','commit':COMMIT,'nonce':(b'1'*16).hex(),'head':head}
    parsed=r.metadata(io.BytesIO((json.dumps(item)+'\n').encode()))
    confirmations.put(parsed)
    assert r.latest_commit(COMMIT)==head
    request=json.loads(capsys.readouterr().out)
    assert request=={'action':'confirm_main','commit':COMMIT,'nonce':item['nonce']}
    confirmations.put(dict(parsed,nonce='f'*32))
    with pytest.raises(ValueError,match='mismatch'):r.latest_commit(COMMIT)


def test_runner_answers_cutover_challenge_without_sending_github_token(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('deployment_client',Path(__file__).with_name('client.py'))
    client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
    image=tmp_path/'image.tar.gz';image.write_bytes(b'synthetic-image')
    nonce='1'*32
    for name,value in {'GH_TOKEN':'synthetic-github-secret','NANZHI_DEPLOY_KEY':'synthetic-ssh-key',
                       'GITHUB_REPOSITORY':r.REPOSITORY,'GITHUB_RUN_ID':'123'}.items():
        monkeypatch.setenv(name,value)
    monkeypatch.setattr(sys,'argv',['client.py','serein',COMMIT,str(image)])
    calls=[]
    def github(args,**kwargs):
        calls.append(args)
        if args[-1]=='.object.sha':return COMMIT+'\n'
        return json.dumps({'artifacts':[{'name':'nanzhi-image-'+COMMIT,'expired':False,
              'archive_download_url':'https://api.github.com/repos/'+r.REPOSITORY+'/actions/artifacts/1/zip','size_in_bytes':42}]})
    monkeypatch.setattr(client.subprocess,'check_output',github)
    class Opener:
        def open(self,*args,**kwargs):
            raise urllib.error.HTTPError('https://api.github.com',302,'redirect',{'Location':'https://test.blob.core.windows.net/artifact'},None)
    monkeypatch.setattr(client.urllib.request,'build_opener',lambda *_:Opener())
    class Process:
        def __init__(self,*args,**kwargs):
            self.stdin=io.BytesIO()
            self.stdout=io.BytesIO((json.dumps({'action':'confirm_main','commit':COMMIT,'nonce':nonce})+'\n'+json.dumps({'phase':'deployed'})+'\n').encode())
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def wait(self,**_):return 0
        def kill(self):pass
    process=Process();monkeypatch.setattr(client.subprocess,'Popen',lambda *_a,**_k:process)
    client.main()
    sent=[json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert sent[0]['action']=='deploy_url'
    assert sent[1]=={'action':'main_confirmation','commit':COMMIT,'nonce':nonce,'head':COMMIT}
    assert b'synthetic-github-secret' not in process.stdin.getvalue()
    assert sum(args[-1]=='.object.sha' for args in calls)==2


def test_only_reviewed_archives_are_excluded_and_originals_survive(tmp_path,monkeypatch):
    source=tmp_path/'data';source.mkdir()
    archive=source/'runtime/user-repair-20261107-2158';archive.mkdir(parents=True)
    (archive/'before.sqlite').write_bytes(b'synthetic old backup')
    live=source/'runtime/serein.db';live.write_bytes(b'synthetic live data')
    (source/'runtime/user-profile.json').write_text('{}')
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({str(source.resolve()):['runtime/user-repair-20261107-2158']}))
    monkeypatch.setattr(r,'ARCHIVE_POLICY',policy)
    monkeypatch.setattr(r.os,'chown',lambda *_a,**_k:None,raising=False)
    job=tmp_path/'job';job.mkdir();dest=job/'test-data-serein-memory-0'
    r.snapshot(source,dest)
    assert (dest/'runtime/serein.db').read_bytes()==live.read_bytes()
    assert (dest/'runtime/user-profile.json').exists()
    assert not (dest/'runtime/user-repair-20261107-2158').exists()
    r.cleanup_rehearsal(job)
    assert not dest.exists() and live.exists() and archive.exists()
    policy.write_text(json.dumps({str(source.resolve()):['runtime/serein.db']}))
    with pytest.raises(ValueError,match='historical_archive'):r.archive_exclusions(source)


def snapshot_sources(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir()
    dest=tmp_path/'copy'
    monkeypatch.setattr(r,'ARCHIVE_POLICY',tmp_path/'no-policy.json')
    monkeypatch.setattr(r.os,'chown',lambda *_a,**_k:None,raising=False)
    return source,dest


def test_snapshot_ignores_only_verified_sqlite_sidecars_that_disappear(tmp_path,monkeypatch):
    source,dest=snapshot_sources(tmp_path,monkeypatch)
    database=source/'memory.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)')
        conn.execute('INSERT INTO evidence VALUES (1,?)',('synthetic original',))
    originals=database.read_bytes()
    companions=[Path(str(database)+suffix) for suffix in ('-wal','-shm','-journal')]
    for path in companions:path.write_bytes(b'synthetic disappearing sidecar')
    retained={'profile.json':b'{"keep":true}', 'not-a-database-wal':b'ordinary wal-named file',
              'not-a-database-shm':b'ordinary shm-named file', 'not-a-database-journal':b'ordinary journal-named file'}
    for name,content in retained.items():(source/name).write_bytes(content)
    native_copytree=r.shutil.copytree;vanished=[]
    def race(src,dst,**kwargs):
        original_ignore=kwargs['ignore']
        def after_enumeration(folder,names):
            if Path(folder)==source:
                for path in companions:
                    assert path.name in names
                    path.unlink();vanished.append(path.name)
            return original_ignore(folder,names)
        return native_copytree(src,dst,**dict(kwargs,ignore=after_enumeration))
    monkeypatch.setattr(r.shutil,'copytree',race)
    r.snapshot(source,dest)
    assert len(vanished)==3 and database.read_bytes()==originals
    with sqlite3.connect(dest/'memory.db') as conn:
        assert conn.execute('SELECT * FROM evidence').fetchall()==[(1,'synthetic original')]
        assert conn.execute('PRAGMA quick_check').fetchall()==[('ok',)]
    assert all(not Path(str(dest/'memory.db')+suffix).exists() for suffix in ('-wal','-shm','-journal'))
    assert {name:(dest/name).read_bytes() for name in retained}==retained


def test_snapshot_backups_include_committed_wal_content_without_touching_source(tmp_path,monkeypatch):
    source,dest=snapshot_sources(tmp_path,monkeypatch)
    database=source/'memory.db';conn=sqlite3.connect(database)
    try:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()==('wal',)
        conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY, value BLOB)');conn.commit()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        content=b'committed only in WAL'+bytes(range(256))*20
        conn.execute('INSERT INTO evidence VALUES (1,?)',(content,));conn.commit()
        wal=Path(str(database)+'-wal')
        assert wal.stat().st_size>0
        before={path.name:path.read_bytes() for path in (database,wal)}
        r.snapshot(source,dest)
        assert conn.execute('PRAGMA journal_mode').fetchone()==('wal',)
        assert {path.name:path.read_bytes() for path in (database,wal)}==before
        with sqlite3.connect(dest/'memory.db') as copied:
            assert copied.execute('SELECT * FROM evidence').fetchall()==[(1,content)]
            assert copied.execute('PRAGMA journal_mode').fetchone()==('delete',)
            assert copied.execute('PRAGMA quick_check').fetchall()==[('ok',)]
        assert all(not Path(str(dest/'memory.db')+suffix).exists() for suffix in ('-wal','-shm','-journal'))
    finally:
        conn.close()


def test_snapshot_does_not_swallow_non_sqlite_copy_errors(tmp_path,monkeypatch):
    source,dest=snapshot_sources(tmp_path,monkeypatch)
    document=source/'original.txt';document.write_text('must not vanish silently')
    native_copytree=r.shutil.copytree
    def race(src,dst,**kwargs):
        original_ignore=kwargs['ignore']
        def after_enumeration(folder,names):
            omitted=original_ignore(folder,names)
            document.unlink()
            return omitted
        return native_copytree(src,dst,**dict(kwargs,ignore=after_enumeration))
    monkeypatch.setattr(r.shutil,'copytree',race)
    with pytest.raises(shutil.Error):r.snapshot(source,dest)


def test_snapshot_refuses_existing_destination(tmp_path,monkeypatch):
    source,dest=snapshot_sources(tmp_path,monkeypatch)
    dest.mkdir();(dest/'keep.txt').write_text('existing copy')
    with pytest.raises(FileExistsError):r.snapshot(source,dest)
    assert (dest/'keep.txt').read_text()=='existing copy'


def test_snapshot_rejects_failed_quick_check_and_closes_connections(tmp_path,monkeypatch):
    source,dest=snapshot_sources(tmp_path,monkeypatch)
    database=source/'memory.db'
    with sqlite3.connect(database) as conn:conn.execute('CREATE TABLE originals(id INTEGER)')
    original=database.read_bytes();closed=[];queries=[]
    class Source:
        def backup(self,target,**kwargs):target.path.write_bytes(b'synthetic rejected copy')
        def close(self):closed.append('source')
    class Target:
        def __init__(self,path):self.path=path
        def execute(self,query):queries.append(query);return self
        def fetchone(self):return ('delete',)
        def fetchall(self):return [('synthetic integrity failure',)]
        def close(self):closed.append('target')
    def connect(target,**kwargs):
        return Source() if kwargs.get('uri') else Target(Path(target))
    monkeypatch.setattr(r.sqlite3,'connect',connect)
    with pytest.raises(ValueError,match='sqlite_copy_invalid'):r.snapshot(source,dest)
    assert queries==['PRAGMA journal_mode=DELETE','PRAGMA quick_check']
    assert closed==['target','source'] and database.read_bytes()==original


def test_cleanup_rejects_symlink_job_without_touching_target(tmp_path,monkeypatch):
    target=tmp_path/'real-job';target.mkdir()
    data=target/'test-data-serein-memory-0';data.mkdir();(data/'keep').write_text('retained')
    job=tmp_path/'linked-job'
    try:job.symlink_to(target,target_is_directory=True)
    except OSError:
        native=Path.is_symlink
        monkeypatch.setattr(Path,'is_symlink',lambda path:path==job or native(path))
    with pytest.raises(ValueError,match='unexpected_rehearsal_job'):r.cleanup_rehearsal(job)
    assert (data/'keep').read_text()=='retained'


def test_rehearsal_snapshot_failure_cleans_only_current_job_copies(deployment,monkeypatch):
    doc,images,job,path,gate=deployment;before=path.read_bytes();created=[]
    protected=job/'data-before.tar.gz';protected.write_bytes(b'backup must survive')
    unrelated=job.parent/'other-job'/'test-data-serein-memory-0';unrelated.mkdir(parents=True)
    (unrelated/'keep.txt').write_text('other run')
    for service in r.SERVICES:
        source=Path(doc['services'][service]['volumes'][0]['source']);source.mkdir(parents=True)
        (source/'keep.txt').write_text('production')
    def fail_second(source,dest):
        dest.mkdir();(dest/'partial').write_text('partial rehearsal copy');created.append(dest)
        if len(created)==2:raise OSError('synthetic_snapshot_failure')
    monkeypatch.setattr(r,'snapshot',fail_second)
    monkeypatch.setattr(r,'call',lambda *_a,**_k:pytest.fail('pre-start failure must not call Docker'))
    with pytest.raises(OSError,match='synthetic_snapshot_failure'):r.rehearsal(doc,images,job)
    assert len(created)==2 and all(not dest.exists() for dest in created)
    assert protected.read_bytes()==b'backup must survive' and (unrelated/'keep.txt').read_text()=='other run'
    assert path.read_bytes()==before and not gate.exists()
    assert all((Path(doc['services'][service]['volumes'][0]['source'])/'keep.txt').read_text()=='production' for service in r.SERVICES)


@pytest.mark.parametrize('down_fails',[False,True])
def test_rehearsal_failed_start_cleans_only_after_successful_down(deployment,monkeypatch,down_fails):
    doc,images,job,_,_=deployment;calls=[]
    monkeypatch.setattr(r,'snapshot',lambda source,dest:dest.mkdir())
    def call(args,**kwargs):
        calls.append(args)
        if 'up' in args:raise RuntimeError('synthetic_up_failed')
        if down_fails:raise RuntimeError('synthetic_down_failed')
        return b''
    monkeypatch.setattr(r,'call',call)
    with pytest.raises(RuntimeError,match='synthetic_down_failed' if down_fails else 'synthetic_up_failed'):
        r.rehearsal(doc,images,job)
    assert ['up' if 'up' in args else 'down' for args in calls]==['up','down']
    assert bool(list(job.glob('test-data-*')))==down_fails


def test_backup_excludes_exact_reviewed_path_not_entire_runtime(deployment,monkeypatch,tmp_path):
    doc,_,job,_,_=deployment
    sources=r.writable_sources(doc)
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({str(sources[0]):['runtime/user-repair-20261107-2158']}))
    monkeypatch.setattr(r,'ARCHIVE_POLICY',policy)
    args=r.backup_arguments(sources,job)
    excludes=[a for a in args if a.startswith('--exclude=')]
    assert excludes==['--exclude=data/serein-memory/runtime/user-repair-20261107-2158']
    assert json.loads((job/'excluded-historical-archives.json').read_text())['data/serein-memory']==['runtime/user-repair-20261107-2158']


def test_retry_reuses_only_digest_verified_same_commit_bundle(deployment,monkeypatch,tmp_path):
    base=tmp_path/'deploy';runs=base/'runs';runs.mkdir(parents=True)
    previous=runs/('previous-serein-'+COMMIT[:12]);previous.mkdir()
    archive=previous/'image.tar.gz';bundle(archive)
    monkeypatch.setattr(r,'BASE',base)
    monkeypatch.setattr(r,'call',lambda *_a,**_k:b'')
    monkeypatch.setattr(r,'rehearsal',lambda *_a:None)
    monkeypatch.setattr(r,'promote',lambda _d,_i,_j,record:record('deployed'))
    monkeypatch.setattr(r,'download_artifact',lambda *_a:pytest.fail('Verified bundle should not be downloaded again'))
    monkeypatch.setattr(r.shutil,'disk_usage',lambda _:types.SimpleNamespace(free=10*1024**3))
    item={'action':'deploy_url','commit':COMMIT,'bytes':archive.stat().st_size,
          'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    result=r.deploy(item,None,{})
    assert result['phase']=='deployed' and result['verified_cached_archive']
    assert archive.exists()
