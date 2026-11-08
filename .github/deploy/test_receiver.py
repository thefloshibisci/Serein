"""Synthetic deployment boundary checks; no SSH, Docker or private data."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import types

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
    monkeypatch.setattr(r,'latest_commit',lambda:COMMIT)
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
    monkeypatch.setattr(r,'latest_commit',lambda:'b'*40)
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
