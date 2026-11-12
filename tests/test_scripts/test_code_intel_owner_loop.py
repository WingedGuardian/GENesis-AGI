"""Actual harmless children and scratch SQL exercise owner-before-ACK ordering."""
import os
import runpy
import signal
import sys
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_index_marker_control import _reserved
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _registration


def prepared(queue, tmp_path, evidence, monkeypatch, *, monitor_failure=False, slow=False):
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_owner.py'))
    namespace = namespace['PhysicalOwner'].__init__.__globals__
    claimed, reserved = _reserved(queue, tmp_path)
    proof = SimpleNamespace(**evidence)
    facts = _registration(claimed)['facts']
    latest = {}

    def capture(*, pid):
        current = deepcopy(facts)
        state = proof._process_state(Path('/proc') / str(pid), pid)
        current['reporter'] = dict(pid=pid, start_ticks=state[1], boot_id=reserved['owner']['boot_id'])
        latest.update(facts=current)
        return current

    def cleanup(scope, digest, proof, *, owned_wait):
        return dict(state='quiescent' if owned_wait else 'unresolved', registration_sha256=digest,
                    facts=deepcopy(latest['facts']), populated=0 if owned_wait else 1,
                    owned_wait=owned_wait, observed_ns=time.monotonic_ns())

    manager = SimpleNamespace(deadline=time.monotonic() + 10)
    manager._object = lambda *args: '/org/freedesktop/systemd1/unit/owned'
    manager.registration = lambda unit, current, **kwargs: dict(unit=unit, facts=current,
        invocation_id='1' * 32, stop_usec=90_000_000)
    manager.original = lambda *args, **kwargs: None
    queued = SimpleNamespace(**runpy.run_path(str(Path(namespace['__file__']).with_name('code_intel_queued_worker.py'))))
    value = dict(context=claimed['context'], reservation=reserved,
                 policy=dict(sibling_reserve_bytes=2 * 1024**3, cache_reserve_bytes=2 * 1024**3))
    queued.runtime_inputs = lambda snapshot, modules: (value, None)
    modules = dict(queue=queue, proof=proof, queued=queued,
        unit=SimpleNamespace(systemctl_env=lambda: dict(os.environ)),
        scope=SimpleNamespace(cleanup_observation=cleanup))
    owner = namespace['PhysicalOwner'](modules, claimed['context'], reserved,
        SimpleNamespace(capture=capture), manager, control=lambda *args: owner.foreground.terminate())
    marker, monitor_ready = tmp_path / 'entered', tmp_path / 'monitor-ready'
    program = ('import json,os,pathlib,sys,time;'
        'ack=json.loads(os.fdopen(int(sys.argv[1])).read());'
        'assert pathlib.Path(sys.argv[3]).read_text()=="ready";'
        'assert ack["owner"]["pid"]==os.getppid();'
        'pathlib.Path(sys.argv[2]).write_text("entered");' + ('time.sleep(30)' if slow else ''))
    monkeypatch.setitem(namespace, 'scope_command', lambda snap, mods, fd:
        [sys.executable, '-I', '-c', program, str(fd), str(marker), str(monitor_ready)])
    monitor = ('import sys;sys.exit(1)' if monitor_failure else
        'import os,pathlib,sys;'
        'pathlib.Path(sys.argv[1]).write_text("ready");'
        'os.write(1,b"ready\\n");sys.stdin.buffer.read()')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', monitor, str(monitor_ready)])

    @contextmanager
    def sealed():
        with open('/dev/null', 'rb') as fd:
            yield fd

    snapshot = SimpleNamespace(sealed=sealed)
    return owner, snapshot, marker, claimed, namespace


def observed_children(namespace, monkeypatch):
    children = []
    original = namespace['subprocess']

    class ObservedPopen(original.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

    monkeypatch.setitem(namespace, 'subprocess', SimpleNamespace(**{**vars(original), 'Popen': ObservedPopen}))
    return children


def test_owner_installs_monitor_before_ack_and_no_registration_never_becomes_clean(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, _ = prepared(queue, tmp_path, evidence, monkeypatch)
    result = owner.run(snapshot)
    assert marker.read_text() == 'entered'
    assert result['state'] == 'uncertain' and result['reason'] == 'startup_incomplete'
    assert result['foreground']['returncode'] == 0 and result['monitor']['returncode'] == 0
    status = queue.execution_status(claimed['context'])
    assert status['launch_permission'] == 'cancelled' and status['native_intent'] is None
    assert status['control_closure']['data'] == result
    with pytest.raises(ValueError):
        owner.run(snapshot)


def test_failed_monitor_never_releases_ack_or_preparation(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, _ = prepared(queue, tmp_path, evidence, monkeypatch, monitor_failure=True)
    result = owner.run(snapshot)
    assert not marker.exists()
    assert result['state'] == 'uncertain'
    assert queue.execution_status(claimed['context'])['native_intent'] is None


def test_expired_work_deadline_still_stops_actual_owned_child(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch, slow=True)
    children = observed_children(namespace, monkeypatch)
    owner.work_deadline = time.monotonic() + 10
    send = owner.modules['queued'].StartupGate.send

    def acknowledged(gate, fd, ack):
        send(gate, fd, ack)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.005)
        assert marker.exists(), 'real startup must finish before this fixture expires work'
        owner.work_deadline = time.monotonic() - 1

    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', acknowledged)
    try:
        result = owner.run(snapshot)
        assert marker.exists()
        status = queue.execution_status(claimed['context'])
        assert status['first_stop'] is not None, (
            'expired work returned without its required wall stop',
            owner.foreground.status() is None)
        assert result is not None and result['foreground']['returncode'] == -signal.SIGTERM
        assert result['stop']['reason'] == status['first_stop']['data']['reason'] == 'wall'
        assert owner.final_deadline > owner.work_deadline
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()  # Only actual harmless children created by this fixture.
            child.wait(timeout=5)


def test_expired_unacknowledged_work_stops_child_without_inventing_final_budget(
        queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch, slow=True)
    children = observed_children(namespace, monkeypatch)
    owner.work_deadline = time.monotonic() - 1

    def unavailable(*args):
        raise ValueError('synthetic manager unavailable before startup header')

    owner.manager._object = unavailable
    try:
        assert owner.run(snapshot) is None
        assert not marker.exists() and owner.startup is None and owner.final_deadline is None
        # The controller retained no completion proof. This fixture separately
        # owns the oracle reap and does not publish it as native/control success.
        assert owner.foreground.child.wait(timeout=5) == -signal.SIGTERM
        assert queue.execution_status(claimed['context'])['control_closure'] is None
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def test_automatic_reaping_refuses_before_any_child(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    monkeypatch.setattr(namespace['signal'], 'getsignal', lambda sig: signal.SIG_IGN)
    with pytest.raises(ValueError):
        owner.run(snapshot)
    assert owner.foreground is None and not marker.exists()
    assert queue.execution_status(claimed['context'])['control_closure'] is None


def test_dead_monitor_leader_with_live_output_producer_leaves_control_open(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    original = owner.manager.registration
    owner.manager.registration = lambda *args, **kwargs: {**original(*args, **kwargs), 'stop_usec': 100_000}
    program = ('import os,subprocess,sys;'
        'subprocess.Popen([sys.executable,"-I","-c","import time;time.sleep(.8)"]);'
        'os.write(1,b"ready\\n")')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args: [sys.executable, '-I', '-c', program])
    assert owner.run(snapshot) is None
    assert queue.execution_status(claimed['context'])['control_closure'] is None


def test_stalled_monitor_outlives_same_final_budget_without_closure(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    original = owner.manager.registration
    owner.manager.registration = lambda *args, **kwargs: {**original(*args, **kwargs), 'stop_usec': 100_000}
    release = tmp_path / 'release-sampler'
    program = ('import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");'
        'os.write(1,b"ready\\n");p=pathlib.Path(sys.argv[2]);'
        '\nwhile not p.exists(): time.sleep(.01)\n'
        'sys.stdin.buffer.read()\n')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', program, str(tmp_path / 'monitor-ready'), str(release)])
    original_subprocess = namespace['subprocess']
    children = observed_children(namespace, monkeypatch)
    try:
        assert owner.run(snapshot) is None
        assert marker.exists() and owner.final_deadline < owner.work_deadline
        with pytest.raises(original_subprocess.TimeoutExpired):
            children[-1].wait(timeout=1)  # The controlled sampler still has no release.
        assert queue.execution_status(claimed['context'])['control_closure'] is None
    finally:
        release.write_text('release')
        for child in children:
            child.wait(timeout=3)


def test_late_retired_monitor_action_is_drained_without_dispatch(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, _, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    program = ('import os,pathlib,sys;'
        'pathlib.Path(sys.argv[1]).write_text("ready");'
        'os.write(1,b"ready\\n");sys.stdin.buffer.read();os.write(1,b"freeze\\n")')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', program, str(marker.parent / 'monitor-ready')])
    actions = []
    original = owner.control

    def control(action, **kwargs):
        actions.append(action)
        return original(action, **kwargs)

    monkeypatch.setattr(owner, 'control', control)
    result = owner.run(snapshot)
    assert marker.exists() and result['foreground']['returncode'] == 0
    assert result['monitor']['returncode'] == 0 and not actions


def test_unsolicited_zero_exit_after_synthetic_registration_is_uncertain(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch, slow=True)
    owner.work_deadline = time.monotonic() + 10
    original = owner.modules['queued'].StartupGate.send

    def send(gate, fd, ack):
        original(gate, fd, ack)
        # Synthetic journal fixture isolates CONTROL classification; it does
        # not certify the separately tested live reporter registration path.
        assert queue.register_scope(claimed['context'], ack['scope']) == 'registered'

    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', send)
    program = ('import os,pathlib,sys,time;pathlib.Path(sys.argv[2]).write_text("ready");'
        'os.write(1,b"ready\\n");'
        'p=pathlib.Path(sys.argv[1]);'
        '\nwhile not p.exists(): time.sleep(.01)\n'
        'pathlib.Path(sys.argv[3]).write_text("unsolicited exit")\n')
    witness = marker.parent / 'monitor-exit'
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', program, str(marker), str(marker.parent / 'monitor-ready'), str(witness)])
    result = owner.run(snapshot)
    assert marker.exists() and witness.read_text() == 'unsolicited exit' and result['monitor']['returncode'] == 0
    assert result['state'] == 'uncertain' and result['reason'] == 'monitor_failed'
    assert queue.execution_status(claimed['context'])['control_closure']['data'] == result


def test_monitor_death_during_manager_read_prevents_startup_ack(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, marker, _, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    pid_file = marker.parent / 'owned-monitor-pid'
    ready = marker.parent / 'monitor-ready'
    program = ('import os,pathlib,signal,sys,time;'
        'signal.signal(signal.SIGTERM,lambda *args:sys.exit(0));'
        'os.write(1,b"ready\\n");pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));'
        'pathlib.Path(sys.argv[2]).write_text("ready");time.sleep(30)')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *args:
        [sys.executable, '-I', '-c', program, str(pid_file), str(ready)])
    registration, send = owner.manager.registration, owner.modules['queued'].StartupGate.send
    calls = []

    def manager_read(*args, **kwargs):
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        calls.append('manager')
        if len(calls) == 1:
            raise ValueError('first bounded manager read unavailable')
        if len(calls) > 2:
            return registration(*args, **kwargs)
        pid = int(pid_file.read_text())
        assert owner.proof._process_state(Path('/proc') / str(pid), pid)[2] == os.getpid()
        os.kill(pid, signal.SIGTERM)  # Exactly the real monitor this fixture created.
        while time.monotonic() < deadline:
            state = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()[0]
            if state == 'Z':
                break
            time.sleep(.01)
        assert state == 'Z'
        return registration(*args, **kwargs)

    def delivered(gate, fd, ack):
        send(gate, fd, ack)
        # Deterministically expose a stale pre-manager-read alive decision.
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)

    monkeypatch.setattr(owner.manager, 'registration', manager_read)
    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', delivered)
    owner.run(snapshot)
    assert not marker.exists(), 'a monitor known dead before ACK cannot release preparation'
