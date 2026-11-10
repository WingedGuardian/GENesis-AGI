"""Owned harmless children expose lifecycle ordering; no manager is contacted."""
import os
import select
import signal
import subprocess
import sys
import time

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_monitor_retirement import (
    completed,
    ready,
    retire,
)
from tests.test_scripts.test_code_intel_monitor_retirement import (
    monitors as monitors,
)
from tests.test_scripts.test_code_intel_owned_child import toy as toy
from tests.test_scripts.test_code_intel_owner_loop import observed_children, prepared
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


@pytest.mark.parametrize('phase', ['sleep', 'retirement'])
def test_default_monitor_eof_fits_small_accepted_final_budget(monitors, phase):
    run = monitors(interval='15', phase=phase)
    ready(run)
    assert select.select([run['child'].stderr], [], [], 5)[0], 'actual wait boundary was not reached'
    assert run['child'].stderr.readline() == b'phase\n'
    retire(run)
    completed(run, timeout=2)  # A valid one-second manager stop gives two seconds.


def reap(children):
    for child in children:
        if child.poll() is None:
            child.terminate()  # Only the actual harmless fixture children.
        child.wait(timeout=5)


@pytest.mark.parametrize('requests', [('freeze',), ('freeze', 'thaw'), ('thaw', 'freeze')])
def test_pressure_before_transient_startup_is_staged_before_ack(
        queue, tmp_path, evidence, monkeypatch, requests):
    owner, snapshot, marker, _, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    children = observed_children(namespace, monkeypatch)
    monitor_ready = tmp_path / 'monitor-ready'
    frames = '\n'.join(('ready', *requests)) + '\n'
    program = ('import os,pathlib,sys;os.write(1,sys.argv[2].encode());'
        'pathlib.Path(sys.argv[1]).write_text("ready");sys.stdin.buffer.read()')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', program, str(monitor_ready), frames])
    registration = owner.manager.registration
    observations, actions = [], []

    def first_unavailable(*args, **kwargs):
        observations.append(True)
        if len(observations) == 1:
            deadline = time.monotonic() + 5
            while (not monitor_ready.exists() or monitor_ready.read_text() != 'ready') and time.monotonic() < deadline:
                time.sleep(.005)
            assert monitor_ready.read_text() == 'ready', 'monitor must publish before the transient error'
            raise ValueError('synthetic transient manager observation')
        return registration(*args, **kwargs)

    def control(action, unit, remaining):
        actions.append(action)
        if action == 'stop':
            owner.foreground.terminate()

    send = owner.modules['queued'].StartupGate.send

    def acknowledged(gate, fd, ack):
        assert actions == [requests[-1]], 'latest pressure state must precede preparation ACK'
        send(gate, fd, ack)

    monkeypatch.setattr(owner.manager, 'registration', first_unavailable)
    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', acknowledged)
    owner._control = control
    try:
        result = owner.run(snapshot)
        assert marker.exists(), 'pressure during transient startup cancelled preparation'
        assert marker.read_text() == 'entered'
        assert result is not None and result['stop'] is None
        assert actions == [requests[-1]]
    finally:
        reap(children)


@pytest.mark.parametrize('stop', ['wall', 'pressure_stop'])
def test_stop_before_startup_cancels_the_actual_unacknowledged_child(
        queue, tmp_path, evidence, monkeypatch, stop):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    children = observed_children(namespace, monkeypatch)
    monitor_ready = tmp_path / 'monitor-ready'
    program = ('import os,pathlib,sys;os.write(1,("ready\\n"+sys.argv[2]+"\\n").encode());'
        'pathlib.Path(sys.argv[1]).write_text("ready");sys.stdin.buffer.read()')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', program, str(monitor_ready), stop])

    def unavailable(*args):
        deadline = time.monotonic() + 5
        while not monitor_ready.exists() and time.monotonic() < deadline:
            time.sleep(.005)
        assert monitor_ready.exists()
        raise ValueError('synthetic manager remains unavailable')

    owner.manager._object = unavailable
    owner.work_deadline = time.monotonic() + 2
    owner._control = lambda *args: pytest.fail('unknown scope cannot grant a manager action')
    try:
        result = owner.run(snapshot)
        assert owner.foreground.status() == -signal.SIGTERM, 'stop request abandoned its actual waiting child'
        assert not marker.exists() and owner.startup is None and owner.final_deadline is None
        assert result is None or result['state'] == 'uncertain'
        assert queue.execution_status(claimed['context'])['native_intent'] is None
    finally:
        reap(children)


@pytest.mark.parametrize('stop', ['wall', 'pressure_stop'])
@pytest.mark.parametrize('late', ['freeze', 'thaw'])
def test_buffered_pressure_after_stop_does_not_invent_monitor_failure(
        queue, tmp_path, evidence, monkeypatch, stop, late):
    owner, snapshot, marker, claimed, namespace = prepared(
        queue, tmp_path, evidence, monkeypatch, slow=True)
    children = observed_children(namespace, monkeypatch)
    monitor_ready, release = tmp_path / 'monitor-ready', tmp_path / 'release-requests'
    frames = stop + '\n' + late + '\n'
    program = ('import os,pathlib,sys,time;'
        'pathlib.Path(sys.argv[1]).write_text("ready");os.write(1,b"ready\\n");'
        'p=pathlib.Path(sys.argv[2]);'
        '\nwhile not p.exists(): time.sleep(.005)\n'
        'os.write(1,sys.argv[3].encode());sys.stdin.buffer.read()')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', program, str(monitor_ready), str(release), frames])
    send = owner.modules['queued'].StartupGate.send

    def acknowledged(gate, fd, ack):
        # Deliberately synthetic registration isolates CONTROL classification;
        # actual native reporter/intent wiring has separate sealed E2E.
        assert queue.register_scope(claimed['context'], ack['scope']) == 'registered'
        send(gate, fd, ack)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.005)
        assert marker.exists(), 'fixture foreground must enter before stop requests'
        release.write_text('release')

    actions = []

    def control(action, unit, remaining):
        actions.append(action)
        assert action == 'stop'
        owner.foreground.terminate()

    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', acknowledged)
    owner._control = control
    try:
        result = owner.run(snapshot)
        assert result is not None and result['state'] == 'clean' and result['reason'] == 'normal'
        assert result['stop']['reason'] == stop and actions == ['stop']
        assert queue.execution_status(claimed['context'])['control_closure']['data'] == result
    finally:
        release.write_text('release on assertion/error')
        reap(children)


@pytest.mark.parametrize('action', ['freeze', 'thaw'])
@pytest.mark.parametrize('cancel_signal', [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, None])
def test_signal_during_manager_wait_enters_validated_stop_before_work_expiry(
        queue, tmp_path, evidence, monkeypatch, action, cancel_signal):
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    owner, snapshot, marker, claimed, namespace = prepared(
        queue, tmp_path, evidence, monkeypatch, slow=True)
    children = observed_children(namespace, monkeypatch)
    monitor_ready = tmp_path / 'monitor-ready'
    release, sent = tmp_path / 'client-completed', tmp_path / 'signal-sent'
    stopped = tmp_path / 'stop-client-completed'
    if cancel_signal is None:
        # Keep the actual foreground alive until the healthy slow client ends.
        foreground = ('import json,os,pathlib,sys,time;'
            'ack=json.loads(os.fdopen(int(sys.argv[1])).read());'
            'assert ack["owner"]["pid"]==os.getppid();'
            'assert pathlib.Path(sys.argv[3]).read_text()=="ready";'
            'pathlib.Path(sys.argv[2]).write_text("entered");p=pathlib.Path(sys.argv[4]);'
            '\nwhile not p.exists(): time.sleep(.005)\n')
        monkeypatch.setitem(namespace, 'scope_command', lambda snap, mods, fd:
            [sys.executable, '-I', '-c', foreground, str(fd), str(marker), str(monitor_ready), str(release)])
    program = ('import os,pathlib,sys,time;'
        'pathlib.Path(sys.argv[1]).write_text("ready");os.write(1,b"ready\\n");'
        'p=pathlib.Path(sys.argv[2]);'
        '\nwhile not p.exists(): time.sleep(.005)\n'
        'os.write(1,(sys.argv[3]+"\\n").encode());sys.stdin.buffer.read()')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', program, str(monitor_ready), str(marker), action])
    send = owner.modules['queued'].StartupGate.send

    def acknowledged(gate, fd, ack):
        assert queue.register_scope(claimed['context'], ack['scope']) == 'registered'
        owner.work_deadline = time.monotonic() + 3
        send(gate, fd, ack)

    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', acknowledged)
    actual_popen, actual_module = subprocess.Popen, namespace['subprocess']
    waits, actions, stop_times = [], [], []

    class HarmlessControl(actual_popen):
        def __init__(self, argv, **kwargs):
            assert argv[:2] == ['/usr/bin/systemctl', '--user']
            assert argv[2] in (action, 'stop') and argv[3:] == [owner.reserved['unit'] + '.scope']
            assert kwargs['close_fds'] is True
            # Replace every attempted manager spawn, including a mutant's;
            # the test can never freeze or stop a live systemd unit.
            if argv[2] == 'stop':
                substitute = [sys.executable, '-I', '-c',
                    'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("complete")', str(stopped)]
            elif cancel_signal is None:
                substitute = [sys.executable, '-I', '-c',
                    'import pathlib,sys,time;time.sleep(.4);pathlib.Path(sys.argv[1]).write_text("complete")',
                    str(release)]
            else:
                substitute = [sys.executable, '-I', '-c',
                    'import os,pathlib,signal,sys,time;'
                    'os.kill(os.getppid(),int(sys.argv[1]));'
                    'p=pathlib.Path(sys.argv[2]);t=p.with_suffix(".tmp");'
                    't.write_text(str(time.monotonic_ns()));t.replace(p);time.sleep(10)',
                    str(int(cancel_signal)), str(sent)]
            super().__init__(substitute, **kwargs)
            waits.append(self)
            if argv[2] != 'stop' and cancel_signal is not None:
                deadline = time.monotonic() + 1
                while not sent.exists() and time.monotonic() < deadline:
                    time.sleep(.005)
                assert sent.exists(), 'owned client must record its actual sent signal'
            if argv[2] != 'stop' and cancel_signal == signal.SIGTERM:
                # A second handled signal arrives before Popen construction
                # returns; a handler must record it without losing this child.
                os.kill(os.getpid(), signal.SIGHUP)

    def control(request, unit, remaining):
        actions.append(request)
        if request == 'stop':
            assert unit == owner.reserved['unit']
            stop_times.append(time.monotonic_ns())
        assert request in (action, 'stop')
        try:
            # Both run() and a direct Popen() use the same harmless substitute.
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, 'Popen', HarmlessControl)
                namespace['subprocess'] = subprocess
                owner._systemctl(request, unit, remaining)
        finally:
            namespace['subprocess'] = actual_module
        if request == 'stop':
            assert stopped.read_text() == 'complete', 'pending cancellation must not cancel the stop client'
            owner.foreground.terminate()

    owner._control = control
    try:
        result = owner.run(snapshot)
        assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
        assert len(waits) == (1 if cancel_signal is None else 2)
        assert all(child.returncode is not None for child in waits)
        assert result is not None
        if cancel_signal is None:
            assert actions == [action] and result['stop'] is None
            assert result['foreground']['returncode'] == 0 and release.read_text() == 'complete'
        else:
            assert actions == [action, 'stop']
            assert result['stop']['reason'] == 'cancellation', 'signal was delayed until the work deadline'
            assert 0 <= stop_times[0] - int(sent.read_text()) < 1_500_000_000
        assert result['state'] == 'clean' and result['reason'] == 'normal'
    finally:
        reap(waits)
        reap(children)
