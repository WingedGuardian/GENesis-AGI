"""Actual harmless children cross injected clocks without renewing budgets."""
import subprocess
import sys
import time

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_owner_loop import observed_children, prepared
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


@pytest.mark.parametrize('action', ['freeze', 'thaw', 'stop'])
@pytest.mark.parametrize('status', [0, 75])
@pytest.mark.parametrize('phase', ['constructor', 'observation'])
@pytest.mark.parametrize('late', [False, True])
def test_manager_reply_must_be_observed_within_original_deadline(
        queue, tmp_path, evidence, monkeypatch, action, status, phase, late):
    owner, _, _, _, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    now = [time.monotonic()]
    owner.clock = lambda: now[0]
    original_deadline = owner.work_deadline = owner.final_deadline = now[0] + 1
    actual_popen, children, observations = subprocess.Popen, [], []
    original_status = owner._control_status

    def crossing():
        now[0] = original_deadline + (.1 if late else -.1)

    class HarmlessClient(actual_popen):
        def __init__(self, argv, **kwargs):
            assert argv == ['/usr/bin/systemctl', '--user', action, owner.reserved['unit'] + '.scope']
            super().__init__([sys.executable, '-I', '-c', f'raise SystemExit({status})'], **kwargs)
            children.append(self)
            # An actual terminal wait supplies the cached result. This fixture
            # never substitutes an invented exit code or contacts a manager.
            assert self.wait(timeout=5) == status
            if phase == 'constructor':
                crossing()

    def observed(child):
        result = original_status(child)
        if result is not None:
            observations.append(result)
            if phase == 'observation':
                crossing()
        return result

    monkeypatch.setattr(owner, '_control_status', observed)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, 'Popen', HarmlessClient)
            patch.setitem(namespace, 'subprocess', subprocess)
            if late:
                with pytest.raises(subprocess.TimeoutExpired):
                    owner._systemctl(action, owner.reserved['unit'], 1)
            elif status:
                with pytest.raises(subprocess.CalledProcessError) as error:
                    owner._systemctl(action, owner.reserved['unit'], 1)
                assert error.value.returncode == status
            else:
                owner._systemctl(action, owner.reserved['unit'], 1)
        assert observations and len(children) == 1 and children[0].returncode == status
        assert owner._control_clients_drained()
        assert owner.work_deadline == owner.final_deadline == original_deadline
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize('late', [False, True])
def test_owner_closure_requires_timely_physical_observation_after_eof(
        queue, tmp_path, evidence, monkeypatch, late):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    children = observed_children(namespace, monkeypatch)
    now = [time.monotonic()]
    owner.clock = lambda: now[0]
    owner.work_deadline = now[0] + 100
    original_registration = owner.manager.registration
    owner.manager.registration = lambda *args, **kwargs: {
        **original_registration(*args, **kwargs), 'stop_usec': 1_000_000}
    send = owner.modules['queued'].StartupGate.send

    def acknowledged(gate, fd, ack):
        assert queue.register_scope(claimed['context'], ack['scope']) == 'registered'
        send(gate, fd, ack)

    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', acknowledged)
    records, owned = namespace['OwnerRecords'], namespace['OwnedChild']
    original_eof, original_status = records.eof, owned.status
    delivered, crossings = [], []

    def eof(channel):
        original_eof(channel)
        delivered.append(True)

    def observed(child):
        result = original_status(child)
        if delivered and child is not owner.foreground and result is not None:
            assert owner.final_deadline is not None
            now[0] = owner.final_deadline + (.1 if late else -.1)
            crossings.append(now[0])
        return result

    monkeypatch.setattr(records, 'eof', eof)
    monkeypatch.setattr(owned, 'status', observed)
    try:
        result = owner.run(snapshot)
        assert marker.read_text() == 'entered' and delivered and crossings
        status = queue.execution_status(claimed['context'])
        if late:
            assert result is None
            assert status['control_closure'] is None
        else:
            assert result['state'] == 'clean' and result['reason'] == 'normal'
            assert status['control_closure']['data'] == result
        assert owner.work_deadline > owner.final_deadline
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
