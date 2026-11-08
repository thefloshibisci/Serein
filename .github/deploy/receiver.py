#!/usr/bin/env python3
"""Forced SSH receiver. Each key is bound to one service by authorized_keys."""
import copy
import concurrent.futures
import fcntl
import hashlib
import json
import os
import queue
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile

BASE = Path('/opt/nanzhi-deploy')
PRODUCTION = Path('/opt/nanzhi-production')
COMPOSE = PRODUCTION / 'compose.json'
SERVICES = ('serein-memory', 'serein')
REPOSITORY = 'thefloshibisci/Serein'
MAINTENANCE = Path('/var/lib/nanzhi-deploy-gates/serein.maintenance')
MAX_ARCHIVE = 2 * 1024**3
CONFIRMATIONS = queue.Queue()


def call(args, timeout=180, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
    if result.returncode:
        # Commands may contain service configuration; never print command/env/logs.
        raise RuntimeError('subprocess_failed_' + Path(args[0]).name)
    return result.stdout


def metadata(stream):
    line = stream.readline(8193)
    if len(line) > 8192 or not line.endswith(b'\n'):
        raise ValueError('invalid_header_length')
    item = json.loads(line)
    if item == {'action': 'check'}:
        return item
    if item.get('action') == 'main_confirmation':
        if (set(item) != {'action','commit','nonce','head'}
                or not re.fullmatch('[0-9a-f]{40}', str(item.get('commit')))
                or not re.fullmatch('[0-9a-f]{40}', str(item.get('head')))
                or not re.fullmatch('[0-9a-f]{32}', str(item.get('nonce')))):
            raise ValueError('invalid_main_confirmation')
        return item
    fields = {'action', 'commit', 'bytes', 'sha256'}
    if item.get('action') == 'deploy_url':
        fields |= {'url', 'artifact_bytes'}
        url = urllib.parse.urlsplit(item.get('url', ''))
        if url.scheme != 'https' or not (url.hostname or '').endswith('.blob.core.windows.net') or url.username or url.password or url.port not in (None, 443):
            raise ValueError('untrusted_artifact_url')
        if type(item.get('artifact_bytes')) is not int or not 0 < item['artifact_bytes'] <= MAX_ARCHIVE:
            raise ValueError('invalid_artifact_size')
    if set(item) != fields or item['action'] not in ('deploy', 'deploy_url'):
        raise ValueError('invalid_action')
    if not re.fullmatch('[0-9a-f]{40}', item['commit']):
        raise ValueError('invalid_commit')
    if not re.fullmatch('[0-9a-f]{64}', item['sha256']):
        raise ValueError('invalid_digest')
    if type(item['bytes']) is not int or not 0 < item['bytes'] <= MAX_ARCHIVE:
        raise ValueError('invalid_size')
    return item


def receive(stream, dest, item):
    remaining = item['bytes']
    digest = hashlib.sha256()
    with dest.open('xb') as output:
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('truncated_archive')
            digest.update(block)
            output.write(block)
            remaining -= len(block)
    if stream.read(1) or digest.hexdigest() != item['sha256']:
        raise ValueError('archive_integrity_failed')


def download_artifact(dest, item, locations=None):
    """Bounded parallel HTTPS ranges tolerate slow cross-region single flows."""
    bundle = dest.with_name('artifact.zip')
    size = item['artifact_bytes']
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    fd = os.open(bundle, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        os.ftruncate(fd, size)
        block = 2 * 1024**2
        ranges = [(start, min(start + block, size) - 1) for start in range(0, size, block)]
        def part(bounds):
            start, end = bounds
            for attempt in range(3):
                try:
                    req = urllib.request.Request((locations or item)['url'], headers={'Range': f'bytes={start}-{end}'})
                    with urllib.request.build_opener(NoRedirect()).open(req, timeout=90) as response:
                        if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{size}':
                            raise ValueError('invalid_artifact_range')
                        data = response.read(end - start + 2)
                    if len(data) != end - start + 1:
                        raise ValueError('truncated_artifact_range')
                    os.pwrite(fd, data, start)
                    return
                except Exception:
                    if attempt == 2:
                        raise RuntimeError('artifact_range_download_failed') from None
                    time.sleep(2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(part, ranges))
    finally:
        os.close(fd)
    with zipfile.ZipFile(bundle) as archive:
        if archive.namelist() != ['image.tar.gz'] or archive.getinfo('image.tar.gz').file_size != item['bytes']:
            raise ValueError('unexpected_artifact_contents')
        digest = hashlib.sha256()
        with archive.open('image.tar.gz') as source, dest.open('xb') as output:
            while data := source.read(1024 * 1024):
                output.write(data)
                digest.update(data)
    if digest.hexdigest() != item['sha256']:
        raise ValueError('artifact_digest_mismatch')
    bundle.unlink()


def snapshot(source, dest):
    """Consistent individual SQLite copies, isolated rehearsal only."""
    shutil.copytree(source, dest, symlinks=True)
    for path in source.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as f:
            is_db = f.read(16) == b'SQLite format 3\0'
        if not is_db:
            continue
        target = dest / path.relative_to(source)
        target.unlink()
        src = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
        dst = sqlite3.connect(target)
        deadline = time.monotonic() + 60
        def progress(*_):
            if time.monotonic() > deadline:
                raise TimeoutError('sqlite_backup_timeout')
        try:
            src.backup(dst, pages=256, progress=progress)
            if dst.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('sqlite_copy_invalid')
        finally:
            dst.close()
            src.close()
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(target) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        shutil.copystat(path, target)
    for d, dirs, files in os.walk(dest):
        original = source / Path(d).relative_to(dest)
        stat = original.stat()
        os.chown(d, stat.st_uid, stat.st_gid)
        for name in files:
            old = original / name
            stat = old.lstat()
            os.chown(Path(d) / name, stat.st_uid, stat.st_gid, follow_symlinks=False)


def compose(path, *args):
    return ['docker', 'compose', '-f', str(path), *args]


def validate_bundle(archive, commit):
    images = {name: 'nanzhi/' + name + ':github-' + commit for name in SERVICES}
    with tarfile.open(archive, 'r:gz') as tar:
        manifest = json.load(tar.extractfile('manifest.json'))
        if len(manifest) != 2 or sorted(row.get('RepoTags') for row in manifest) != sorted([v] for v in images.values()):
            raise ValueError('unexpected_image_manifest')
        for row in manifest:
            config = json.load(tar.extractfile(row['Config']))
            if config.get('architecture') != 'amd64' or config.get('os') != 'linux':
                raise ValueError('unexpected_image_platform')
            if config.get('config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != commit:
                raise ValueError('image_revision_mismatch')
    return images


def writable_sources(doc):
    sources = []
    for name in SERVICES:
        for mount in doc['services'][name]['volumes']:
            source = Path(mount['source']).resolve()
            if not source.is_relative_to(PRODUCTION) or mount['type'] != 'bind':
                raise ValueError('unexpected_mount')
            if not mount.get('read_only'):
                if not source.is_relative_to(PRODUCTION / 'data'):
                    raise ValueError('unexpected_writable_mount')
                sources.append(source)
    if not sources or len(set(sources)) != len(sources):
        raise ValueError('unexpected_shared_data')
    return sources


def inspect_containers(path):
    ids = call(compose(path, 'ps', '-q')).decode().split()
    if not ids:
        raise RuntimeError('missing_containers')
    return {c['Config']['Labels']['com.docker.compose.service']: c
            for c in json.loads(call(['docker', 'inspect', *ids]))}


def check_pair(path):
    containers = inspect_containers(path)
    for name in SERVICES:
        if not containers[name]['State']['Running']:
            raise RuntimeError('container_not_running')
    # Credential is read inside the container. Only booleans/status leave it.
    worker = r'''
import json,os,urllib.request,urllib.error
from pathlib import Path
token=Path(os.environ['SEREIN_HTTP_TOKEN_FILE']).read_text().strip()
def req(path, key=None, body=None, headers=None):
 h={'Accept':'application/json, text/event-stream','Content-Type':'application/json'}
 if key:h['Authorization']='Bearer '+key
 h.update(headers or {})
 r=urllib.request.Request('http://127.0.0.1:8011'+path,headers=h,data=json.dumps(body).encode() if body is not None else None)
 try:res=urllib.request.urlopen(r,timeout=8)
 except urllib.error.HTTPError as e:res=e
 return res.status,res.read(2000000).decode(),dict(res.headers)
assert req('/health')[0]==200
assert req('/v1/capabilities','invalid-deploy-token')[0]==401
assert req('/v1/capabilities',token)[0]==200
b={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'deployment-check','version':'1'}}}
assert req('/mcp','invalid-deploy-token',b)[0]==401
s,body,h=req('/mcp',token,b);assert s==200 and 'protocolVersion' in body
headers={'MCP-Protocol-Version':'2025-06-18'}
for k,v in h.items():
 if k.lower()=='mcp-session-id':headers['Mcp-Session-Id']=v
req('/mcp',token,{'jsonrpc':'2.0','method':'notifications/initialized'},headers)
s,body,_=req('/mcp',token,{'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},headers)
assert s==200 and '"tools"' in body
print('health_auth_mcp_ok')
'''
    call(['docker', 'exec', containers['serein-memory']['Id'], 'python', '-c', worker], timeout=55)
    gateway = """import {readFileSync} from 'node:fs';
const base='http://127.0.0.1:8080';
for(const [path,status] of [['/ready',200],['/',401],['/v1/models',401]]) {
 const r=await fetch(base+path,{signal:AbortSignal.timeout(8000)});if(r.status!==status)throw Error('gateway_check_failed');
}
const token=readFileSync(process.env.SEREIN_MEMORY_TOKEN_FILE,'utf8').trim();
const body={jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'deployment-check',version:'1'}}};
for(const key of ['invalid-deploy-token',token]) {
 const r=await fetch(base+'/mcp',{method:'POST',headers:{Authorization:'Bearer '+key,Accept:'application/json, text/event-stream','Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(8000)});
 if(r.status!==(key===token?200:401))throw Error('gateway_mcp_auth_failed');
 if(key===token && !(await r.text()).includes('protocolVersion'))throw Error('gateway_mcp_invalid');
}
console.log('gateway_ready_auth_mcp_ok');
"""
    call(['docker', 'exec', containers['serein']['Id'], 'node', '--input-type=module', '-e', gateway], timeout=55)


def wait_ready(path, seconds=150):
    deadline = time.monotonic() + seconds
    while True:
        try:
            check_pair(path)
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError('health_auth_mcp_check_failed') from None
            time.sleep(4)


# Initialize the copied DB, then run the real writable API without background
# jobs. The separate Docker network is internal and has no production aliases.
REHEARSAL_BOOT = '''
import os
from pathlib import Path
from contextlib import asynccontextmanager
import serein.lifecycle
@asynccontextmanager
async def no_jobs(application):
    yield
serein.lifecycle.lifespan=no_jobs
from serein.config import load_settings
from serein.bootstrap import initialize
from serein.api.http import create_app
import uvicorn
settings=load_settings('/config/config.toml')
initialize(settings)
token=Path(os.environ['SEREIN_HTTP_TOKEN_FILE']).read_text().strip()
uvicorn.run(create_app(settings,token=token,live=True),host='0.0.0.0',port=8011)
'''


def rehearsal_document(doc, images, job):
    test_services = {}
    for name in SERVICES:
        svc = copy.deepcopy(doc['services'][name])
        svc.update(image=images[name], restart='no', ports=[], networks=['isolated'])
        svc.pop('depends_on', None)
        svc.pop('container_name', None)
        for index, mount in enumerate(svc['volumes']):
            if not mount.get('read_only'):
                mount['source'] = str(job / ('test-data-' + name + '-' + str(index)))
        if name == 'serein-memory':
            svc['entrypoint'] = ['python', '-c']
            svc['command'] = [REHEARSAL_BOOT]
        else:
            svc['environment']['SEREIN_MEMORY_URL'] = 'http://serein-memory:8011'
            svc['environment']['SEREIN_MEMORY_HOST'] = 'serein-memory'
            svc['environment']['SEREIN_CORE_HOST'] = 'serein-memory'
        test_services[name] = svc
    return {'name': 'serein-deploy-check', 'services': test_services,
            'networks': {'isolated': {'internal': True}}}


def rehearsal(doc, images, job):
    writable_sources(doc)
    test = rehearsal_document(doc, images, job)
    for name in SERVICES:
        for old, new in zip(doc['services'][name]['volumes'], test['services'][name]['volumes']):
            if not old.get('read_only'):
                snapshot(Path(old['source']), Path(new['source']))
    path = job / 'test-compose.json'
    path.write_text(json.dumps(test))
    try:
        call(compose(path, 'up', '-d', '--pull', 'never'))
        wait_ready(path)
    finally:
        call(compose(path, 'down', '--timeout', '60'), timeout=150)


def latest_commit(commit):
    # The runner can reach GitHub's authenticated API. Ask over the same
    # service-scoped SSH connection immediately before cutover; no GitHub
    # credential or unreliable server-to-GitHub API call is needed here.
    nonce = os.urandom(16).hex()
    print(json.dumps({'action':'confirm_main','commit':commit,'nonce':nonce}), flush=True)
    try:
        response = CONFIRMATIONS.get(timeout=60)
    except queue.Empty:
        raise RuntimeError('main_confirmation_timeout') from None
    if response['commit'] != commit or response['nonce'] != nonce:
        raise ValueError('main_confirmation_mismatch')
    return response['head']


def promote(doc, images, job, record):
    # Refuse stale builds at the actual cutover, after any slow download/test.
    commit = next(iter(images.values())).split(':github-')[1]
    if latest_commit(commit) != commit:
        record('skipped_main_advanced')
        return
    if MAINTENANCE.exists():
        raise RuntimeError('previous_deployment_needs_recovery')
    before = inspect_containers(COMPOSE)
    old_bytes = COMPOSE.read_bytes()
    if json.loads(old_bytes) != doc:
        raise RuntimeError('compose_changed_during_rehearsal')
    (job / 'compose-before.json').write_bytes(old_bytes)
    sources = writable_sources(doc)
    updated = copy.deepcopy(doc)
    for name in SERVICES:
        updated['services'][name]['image'] = images[name]
    pending = COMPOSE.with_suffix('.serein-pending.json')
    pending.write_text(json.dumps(updated, indent=2))
    call(compose(pending, 'config', '--quiet'))
    MAINTENANCE.touch(mode=0o644)
    os.chmod(MAINTENANCE, 0o644)
    record('quiescing')
    call(compose(COMPOSE, 'stop', '--timeout', '60', *reversed(SERVICES)), timeout=180)
    record('backing_up')
    call(['tar', '-czf', str(job / 'data-before.tar.gz'), '-C', str(PRODUCTION), '--',
          *(str(p.relative_to(PRODUCTION)) for p in sources)], timeout=300)
    call(['tar', '-tzf', str(job / 'data-before.tar.gz')], timeout=120)
    pending.replace(COMPOSE)
    record('starting')
    call(compose(COMPOSE, 'up', '-d', '--no-deps', '--pull', 'never', *SERVICES))
    wait_ready(COMPOSE)
    after = inspect_containers(COMPOSE)
    if any(after.get(name, {}).get('Id') != c['Id'] for name, c in before.items() if name not in SERVICES):
        raise RuntimeError('unrelated_container_changed')
    MAINTENANCE.unlink()
    record('deployed')


def deploy(item, stream, locations):
    if MAINTENANCE.exists():
        raise RuntimeError('previous_deployment_needs_recovery')
    job = BASE / 'runs' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-serein-' + item['commit'][:12])
    job.mkdir(mode=0o700)
    status = {'service': 'serein', 'commit': item['commit'], 'phase': 'receiving'}
    def record(phase):
        status.update(phase=phase, updated_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        (job / 'receipt.json').write_text(json.dumps(status, indent=2))
    try:
        if shutil.disk_usage(BASE).free < max(6 * 1024**3, item['bytes'] * 5):
            raise RuntimeError('insufficient_disk_space')
        archive = job / 'image.tar.gz'
        record('receiving')
        if item['action'] == 'deploy_url':
            download_artifact(archive, item, locations)
        else:
            receive(stream, archive, item)
        images = validate_bundle(archive, item['commit'])
        call(['docker', 'load', '-i', str(archive)], timeout=300)
        doc = json.loads(COMPOSE.read_text())
        if all(doc['services'][name]['image'] == image for name, image in images.items()):
            wait_ready(COMPOSE)
            record('already_deployed')
        else:
            record('isolated_rehearsal')
            rehearsal(doc, images, job)
            record('preflight_passed')
            promote(doc, images, job, record)
        archive.unlink()
        return status
    except Exception as exc:
        status['error'] = type(exc).__name__ + ':' + str(exc)[:120]
        record('failed_' + status['phase'])
        # New code may have migrated the DB: retain maintenance and the frozen
        # backup for deliberate recovery; never silently downgrade live data.
        raise


def main():
    os.umask(0o077)
    signal.alarm(40 * 60)
    if len(sys.argv) != 1:
        raise ValueError('arguments_not_allowed')
    stream = os.fdopen(os.dup(sys.stdin.fileno()), 'rb', buffering=0)
    item = metadata(stream)
    if item['action'] == 'check':
        print(json.dumps({'ready': COMPOSE.is_file(), 'service': 'serein', 'protocol': 1}))
        return
    if item['action'] != 'deploy_url':
        raise ValueError('signed_artifact_required')
    locations = {'url': item.get('url')}
    if item['action'] == 'deploy_url':
        def refresh_locations():
            while True:
                try:
                    renewed = metadata(stream)
                    if renewed['action'] == 'main_confirmation':
                        CONFIRMATIONS.put(renewed)
                        continue
                    if {k:v for k,v in renewed.items() if k != 'url'} != {k:v for k,v in item.items() if k != 'url'}:
                        return
                    locations['url'] = renewed['url']
                except Exception:
                    return
        threading.Thread(target=refresh_locations, daemon=True).start()
    with (BASE / 'deployment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(deploy(item, stream, locations)))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'deployed': False, 'error': type(exc).__name__ + ':' + str(exc)[:120]}))
        sys.exit(1)
