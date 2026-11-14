"""Actual harmless manager clients preserve errors and owned wait authority."""
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_owner_loop import observed_children, prepared
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


@pytest.mark.parametrize('action', ['freeze', 'thaw'])
@pytest.mark.parametrize('status', [0, 1, 75, None])
def test_manager_client_success_nonzero_and_timeout_preserve_their_meaning(
        queue, tmp_path, evidence, monkeypatch, action, status):
    owner, _, _, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    actual_popen, children = subprocess.Popen, []

    class HarmlessClient(actual_popen):
        def __init__(self, argv, **kwargs):
            assert argv == ['/usr/bin/systemctl', '--user', action, owner.reserved['unit'] + '.scope']
            assert kwargs['close_fds'] is True
            program = 'import time;time.sleep(10)' if status is None else f'raise SystemExit({status})'
            super().__init__([sys.executable, '-I', '-c', program], **kwargs)
            children.append(self)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, 'Popen', HarmlessClient)
            patch.setitem(namespace, 'subprocess', subprocess)
            if status is None:
                with pytest.raises(subprocess.TimeoutExpired):
                    owner._systemctl(action, owner.reserved['unit'], .1)
                assert children[0].returncode is not None or children[0] in owner.control_clients
            elif status:
                with pytest.raises(subprocess.CalledProcessError) as failure:
                    owner._systemctl(action, owner.reserved['unit'], 2)
                assert failure.value.returncode == status
            else:
                owner._systemctl(action, owner.reserved['unit'], 2)
            deadline = time.monotonic() + 2
            while not owner._control_clients_drained() and time.monotonic() < deadline:
                time.sleep(.005)
            assert owner._control_clients_drained()
            assert len(children) == 1 and children[0].returncode == (-signal.SIGKILL if status is None else status)
            assert queue.execution_status(claimed['context'])['first_stop'] is None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()  # Only this fixture's actual harmless subprocess.
            child.wait(timeout=5)


def test_lost_actual_control_wait_cannot_become_a_closure(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, namespace = prepared(
        queue, tmp_path, evidence, monkeypatch, slow=True)
    foregrounds = observed_children(namespace, monkeypatch)
    original_registration = owner.manager.registration
    owner.manager.registration = lambda *args, **kwargs: {
        **original_registration(*args, **kwargs), 'stop_usec': 1_000_000}
    ready, stopped = tmp_path / 'monitor-ready', tmp_path / 'stop-replied'
    monitor = ('import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");'
        'os.write(1,b"ready\\n");p=pathlib.Path(sys.argv[2]);'
        '\nwhile not p.exists(): time.sleep(.005)\n'
        'os.write(1,b"freeze\\n");sys.stdin.buffer.read()')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', monitor, str(ready), str(marker)])
    send = owner.modules['queued'].StartupGate.send

    def acknowledged(gate, fd, ack):
        assert queue.register_scope(claimed['context'], ack['scope']) == 'registered'
        send(gate, fd, ack)

    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', acknowledged)
    actual_popen, original_module = subprocess.Popen, namespace['subprocess']
    clients, stolen_waits = [], []

    class HarmlessClient(actual_popen):
        def __init__(self, argv, **kwargs):
            assert argv[:2] == ['/usr/bin/systemctl', '--user']
            assert argv[2] in ('freeze', 'stop') and argv[3:] == [owner.reserved['unit'] + '.scope']
            program = ('pass' if argv[2] == 'freeze' else
                'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("complete")')
            super().__init__([sys.executable, '-I', '-c', program, str(stopped)], **kwargs)
            clients.append(self)
            if argv[2] == 'freeze':
                # This independent fixture actor steals the actual wait,
                # leaving Popen.returncode unset; the owner must see ECHILD.
                pid, status = os.waitpid(self.pid, 0)
                assert pid == self.pid and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
                stolen_waits.append(pid)

    def control(action, unit, remaining):
        try:
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, 'Popen', HarmlessClient)
                namespace['subprocess'] = subprocess
                owner._systemctl(action, unit, remaining)
        finally:
            namespace['subprocess'] = original_module
        if action == 'stop':
            assert stopped.read_text() == 'complete'
            owner.foreground.terminate()

    owner._control = control
    try:
        assert owner.run(snapshot) is None
        assert marker.exists() and stopped.read_text() == 'complete'
        assert len(stolen_waits) == 1 and len(clients) == 2 and clients[1].returncode == 0
        assert queue.execution_status(claimed['context'])['control_closure'] is None
    finally:
        for child in clients + foregrounds:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
