"""Supervisor recovery uses only synthetic children and ephemeral loopback ports."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest


@pytest.fixture
def runtime_module():
    source = Path(__file__).parents[1] / 'scripts' / 'local_runtime.py'
    spec = importlib.util.spec_from_file_location('local_runtime_resilience', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    assert predicate(), 'Synthetic supervisor did not recover'


@pytest.mark.parametrize('exit_code', [0, 7])
def test_supervisor_restarts_with_backoff_and_stop_cancels_recovery(tmp_path, monkeypatch, runtime_module, exit_code):
    deploy = tmp_path / 'deploy'
    (deploy / 'runtime').mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    (deploy / 'installation.json').write_text(json.dumps({'memory_port': port}))
    attempts = tmp_path / 'attempts.txt'
    script = tmp_path / 'synthetic_child.py'
    script.write_text('import sys,time\n'
                      'with open(sys.argv[1], "a") as out: out.write(str(time.monotonic())+"\\n")\n'
                      'sys.exit(int(sys.argv[2]))\n')
    monkeypatch.setattr(runtime_module, 'command', lambda *_:
                        ([sys.executable, str(script), str(attempts), str(exit_code)], dict(os.environ)))
    errors = []
    def supervise():
        try:
            runtime_module.supervise(deploy, 'memory')
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=supervise, daemon=True)
    thread.start()
    def timestamps():
        return [float(line) for line in attempts.read_text().splitlines()] if attempts.exists() else []
    try:
        wait_until(lambda: len(timestamps()) >= 3)
        times = timestamps()
        assert times[1] - times[0] >= 1
        assert times[2] - times[1] >= 2
        wait_until(lambda: (runtime_module.control(deploy, 'memory', 'status') or {}).get('restarting'))
        assert runtime_module.control(deploy, 'memory', 'stop')['ok']
        thread.join(3)
        assert not thread.is_alive()
        assert len(timestamps()) == 3
        assert not (deploy / 'runtime' / 'memory.control.json').exists()
        assert not errors
    finally:
        runtime_module.control(deploy, 'memory', 'stop')
        thread.join(3)


def test_start_reuses_supervisor_during_recovery(tmp_path, monkeypatch, runtime_module):
    states = iter([{'ok': True, 'running': False, 'restarting': True}, {'ok': True, 'running': True}])
    monkeypatch.setattr(runtime_module, 'control', lambda *_: next(states))
    monkeypatch.setattr(runtime_module, 'ready', lambda *_: True)
    def unexpected_spawn(*args, **kwargs):
        pytest.fail('A recovering supervisor must not be duplicated')
    monkeypatch.setattr(runtime_module.subprocess, 'Popen', unexpected_spawn)
    runtime_module.start(tmp_path, 'memory')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX TIME_WAIT port reuse semantics')
def test_probe_accepts_time_wait_but_rejects_live_listener(runtime_module):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0)); listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(OSError):
            runtime_module.check_ports([port])
        with socket.create_connection(('127.0.0.1', port)) as client:
            conn, _ = listener.accept()
            conn.close()  # Server is the active closer, leaving server-side TIME_WAIT.
            assert client.recv(1) == b''
    with socket.socket() as probe:
        with pytest.raises(OSError):
            probe.bind(('127.0.0.1', port))
    runtime_module.check_ports([port])
