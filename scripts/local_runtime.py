"""Local Python/Node services. Control only children owned by this supervisor.

No signals are sent to PIDs loaded from disk. The private loopback control
endpoint authenticates requests; an OS lock prevents duplicate supervisors.
"""
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import importlib.util
from urllib.request import urlopen
from urllib.error import HTTPError


def exclusive_lock(path):
    # The source file is stdlib-only: stop/status must work after a failed pip install.
    source=Path(__file__).resolve().parents[1]/'src'/'serein'/'file_lock.py'
    if source.exists():
        spec=importlib.util.spec_from_file_location('serein_process_lock',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.exclusive_lock(path)
    from serein.file_lock import exclusive_lock as installed_lock
    return installed_lock(path)


def settings(deploy):
    return json.loads((deploy/'installation.json').read_text('utf-8'))


def env_file(deploy):
    return dict(line.split('=', 1) for line in (deploy/'.env').read_text('utf-8').splitlines()
                if '=' in line and not line.startswith('#'))


def control(deploy, service, action):
    try:
        record = json.loads((deploy/'runtime'/f'{service}.control.json').read_text('utf-8'))
        with socket.create_connection(('127.0.0.1', record['port']), timeout=2) as conn:
            conn.sendall(json.dumps({'token': record['token'], 'action': action}).encode()+b'\n')
            return json.loads(conn.makefile('rb').readline(4096))
    except (OSError, ValueError, KeyError):
        return None


def command(deploy, service):
    config = settings(deploy)
    values = env_file(deploy)
    root = deploy.parent
    env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONUTF8='1')
    env.update(SEREIN_HTTP_TOKEN_FILE=str(deploy/'secrets'/'api-token'),
               SEREIN_MEMORY_TOKEN_FILE=str(deploy/'secrets'/'api-token'),
               SEREIN_WEB_AUTH_FILE=str(deploy/'secrets'/'web-auth.json'),
               SEREIN_MEMORY_URL=f"http://127.0.0.1:{config['memory_port']}",
               SEREIN_PREVIEW_PORT=str(config['preview_port']),
               SEREIN_GATEWAY_PORT=values['SEREIN_PORT'],
               SEREIN_GATEWAY_BIND=values['SEREIN_BIND'],
               SEREIN_PUBLIC_ORIGIN=values.get('SEREIN_PUBLIC_ORIGIN',''),
               SEREIN_ROUTE_DRAFT_FILE=str(deploy/'runtime'/'semantic-route-draft.json'))
    # Credentials must come from this instance, never a different shell instance.
    env.pop('SEREIN_HTTP_TOKEN', None)
    env.pop('SEREIN_MEMORY_TOKEN', None)
    if service == 'memory':
        args = [sys.executable, '-m', 'serein.launch', '--config', str(deploy/'config.toml'),
                'http', '--live', '--host', '127.0.0.1', '--port', str(config['memory_port'])]
    else:
        node = shutil.which('node')
        if not node:
            raise ValueError('未找到 Node.js，请安装并加入 PATH。')
        args = [node, str(root/'web'/'server'/'gateway.mjs')]
    return args, env


def ready(deploy, service):
    config = settings(deploy)
    values = env_file(deploy)
    port = config['memory_port'] if service == 'memory' else int(values['SEREIN_PORT'])
    path = '/health' if service == 'memory' else '/'
    try:
        with urlopen(f'http://127.0.0.1:{port}{path}', timeout=2) as response:
            return service == 'memory' and response.status == 200
    except HTTPError as exc:
        return service == 'gateway' and exc.code == 401
    except OSError:
        return False


def check_ports(ports):
    # POSIX reuse permits TIME_WAIT without sharing an active listener. Windows
    # SO_REUSEADDR can share live ports, so retain exclusive ownership there.
    for port in ports:
        with socket.socket() as probe:
            option = socket.SO_EXCLUSIVEADDRUSE if os.name == 'nt' else socket.SO_REUSEADDR
            probe.setsockopt(socket.SOL_SOCKET, option, 1)
            probe.bind(('127.0.0.1', port))


def supervise(deploy, service):
    runtime = deploy/'runtime'
    record = runtime/f'{service}.control.json'
    with exclusive_lock(runtime/f'{service}.service.lock'):
        args, env = command(deploy, service)
        config = settings(deploy)
        ports = [config['memory_port']] if service == 'memory' else [config['preview_port'], int(env_file(deploy)['SEREIN_PORT'])]
        # Fail before creating a child if another instance already owns a port.
        check_ports(ports)
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(4)
            server.settimeout(.25)
            token = secrets.token_urlsafe(32)
            child = subprocess.Popen(args, cwd=deploy.parent, env=env,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            started_at = time.monotonic()
            restart_at = None
            delay = 1
            try:
                fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as out:
                    json.dump({'port': server.getsockname()[1], 'token': token}, out)
                while True:
                    now = time.monotonic()
                    if child.poll() is not None:
                        if restart_at is None:
                            if now - started_at >= 60:
                                delay = 1
                            print(f'{service} exited ({child.returncode}); restarting in {delay}s.', flush=True)
                            restart_at = now + delay
                            delay = min(delay * 2, 30)
                        if now >= restart_at:
                            try:
                                child = subprocess.Popen(args, cwd=deploy.parent, env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                            except OSError as exc:
                                print(f'{service} restart failed: {exc}; retrying in {delay}s.', flush=True)
                                restart_at = now + delay
                                delay = min(delay * 2, 30)
                            else:
                                started_at = now
                                restart_at = None
                                print(f'{service} restarted.', flush=True)
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    with conn:
                        conn.settimeout(2)
                        try:
                            request = json.loads(conn.makefile('rb').readline(4096))
                            if not hmac.compare_digest(str(request.get('token', '')), token):
                                conn.sendall(b'{"ok":false}\n')
                                continue
                            action = request.get('action')
                            running = child.poll() is None
                            conn.sendall(json.dumps({'ok': True, 'running': running,
                                                     'restarting': not running}).encode()+b'\n')
                            if action == 'stop':
                                break
                        except (OSError, ValueError):
                            continue
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                record.unlink(missing_ok=True)


def start(deploy, service):
    current = control(deploy, service, 'status')
    if current and current.get('running'):
        if not ready(deploy, service):
            raise ValueError(f'{service} 进程仍在但未就绪，请查看日志后重启。')
        print(f'{service} 已在运行。')
        return
    logs = deploy/'runtime'/'logs'
    process = None
    # A supervisor in backoff still owns this service. Wait for its recovery
    # instead of spawning a duplicate that would fail the ownership lock.
    if not current or not current.get('ok'):
        logs.mkdir(parents=True, exist_ok=True)
        with (logs/f'{service}.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                '--deploy', str(deploy), 'supervise', service], cwd=deploy.parent,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt')
    for _ in range(120):
        current = control(deploy, service, 'status')
        if current and current.get('running') and ready(deploy, service):
            print(f'{service} 已启动并就绪。')
            return
        if (process is not None and process.poll() is not None) or (process is None and not current):
            break
        time.sleep(.5)
    if process is not None:
        control(deploy, service, 'stop')
    raise ValueError(f'{service} 未就绪，请查看 {logs/service}.log')


def stop(deploy, service):
    reply = control(deploy, service, 'stop')
    if reply and reply.get('ok'):
        for _ in range(90):
            if not (deploy/'runtime'/f'{service}.control.json').exists():
                print(f'{service} 已停止。')
                return
            time.sleep(.25)
        raise ValueError(f'{service} 停止未完成，请查看日志。')
    # No live supervisor: don't guess a PID or kill an unrelated process.
    with exclusive_lock(deploy/'runtime'/f'{service}.service.lock'):
        print(f'{service} 未运行。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deploy', type=Path, required=True)
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status', 'supervise'])
    parser.add_argument('service', choices=['memory', 'gateway', 'all'], default='all', nargs='?')
    args = parser.parse_args()
    deploy = args.deploy.resolve()
    if args.action == 'supervise':
        if args.service == 'all':
            parser.error('supervise requires one service')
        supervise(deploy, args.service)
        return
    with exclusive_lock(deploy/'runtime'/'service-command.lock'):
        operate(deploy,args.action,args.service)


def operate(deploy,action,selected):
    services = ['memory', 'gateway'] if selected == 'all' else [selected]
    if action in ('stop', 'restart'):
        for service in reversed(services):
            stop(deploy, service)
    if action in ('start', 'restart'):
        for service in services:
            start(deploy, service)
    if action == 'status':
        for service in services:
            current = control(deploy, service, 'status')
            print(f"{service}: " + ('就绪' if current and current.get('running') and ready(deploy, service) else '未运行或未就绪'))


if __name__ == '__main__':
    main()
