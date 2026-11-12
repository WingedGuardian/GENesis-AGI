"""Real toy children and scratch SQL; manager/kernel/registration are fixtures."""
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_owner_loop import observed_children, prepared
from tests.test_scripts.test_code_intel_owner_review_transitions import reap
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


def stop_case(queue, tmp_path, evidence, monkeypatch, reason):
    owner, snapshot, marker, claimed, namespace = prepared(
        queue, tmp_path, evidence, monkeypatch, slow=True)
    children = observed_children(namespace, monkeypatch)
    registration = owner.manager.registration
    owner.manager.registration = lambda *a, **kw: {**registration(*a, **kw), 'stop_usec': 500_000}
    ready = tmp_path / 'monitor-ready'
    program = ('import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");'
        'os.write(1,b"ready\\n");p=pathlib.Path(sys.argv[2]);'
        '\nwhile not p.exists(): time.sleep(.005)\n'
        'os.write(1,b"pressure_stop\\n") if sys.argv[3]=="pressure_stop" else None;sys.stdin.buffer.read()')
    monkeypatch.setitem(namespace, 'monitor_command', lambda *a:
        [sys.executable, '-I', '-c', program, str(ready), str(marker), reason])
    send = owner.modules['queued'].StartupGate.send

    def acknowledged(gate, fd, ack):
        assert queue.register_scope(claimed['context'], ack['scope']) == 'registered'
        send(gate, fd, ack)
        until = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < until:
            time.sleep(.005)
        assert marker.exists()
        if reason == 'wall':
            owner.work_deadline = time.monotonic() - 1
        if reason == 'cancellation':
            owner.cancelled.append(signal.SIGTERM)

    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', acknowledged)
    return owner, snapshot, claimed, children


def attempts(owner):
    original, records = owner.control, []

    def attempt(action, *, reason=None):
        record = dict(action=action, reason=reason)
        records.append(record)
        try:
            return original(action, reason=reason)
        finally:
            record['deadline'] = owner.final_deadline

    owner.control = attempt
    return records


@pytest.mark.parametrize('reason', ['wall', 'cancellation', 'pressure_stop'])
@pytest.mark.parametrize('failure', ['observation', 'nonzero', 'timeout'])
def test_failed_stop_retries_without_replacing_reason_or_deadline(
        queue, tmp_path, evidence, monkeypatch, reason, failure):
    owner, snapshot, claimed, children = stop_case(queue, tmp_path, evidence, monkeypatch, reason)
    records, actions = attempts(owner), []
    current = owner._current

    def observation():
        if failure == 'observation' and len(records) == 1:
            owner.cancelled.append(signal.SIGTERM)
            owner.work_deadline = time.monotonic() - 1
            raise OSError('synthetic first observation refusal')
        return current()

    def control(action, unit, remaining):
        actions.append(action)
        assert action == 'stop' and unit == owner.reserved['unit']
        if len(actions) == 1:
            owner.cancelled.append(signal.SIGTERM)
            owner.work_deadline = time.monotonic() - 1
            if failure == 'nonzero':
                raise subprocess.CalledProcessError(75, ['harmless-control'])
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(['harmless-control'], remaining)
        owner.foreground.terminate()

    owner._current, owner._control = observation, control
    try:
        result = owner.run(snapshot)
        assert len(records) == 2, ('failed stop was not retried', owner.diagnostics)
        assert {r['reason'] for r in records} == {reason}
        assert records[0]['deadline'] == records[1]['deadline'] == owner.final_deadline
        assert actions == ['stop'] * (1 if failure == 'observation' else 2)
        assert result is not None and result['foreground']['returncode'] == -signal.SIGTERM
        assert result['stop']['reason'] == reason
        state = queue.execution_status(claimed['context'])
        assert state['control_closure']['data'] == result and state['native_intent'] is None
    finally:
        reap(children)


def test_natural_physical_end_after_refusal_does_not_invent_a_stop(
        queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, claimed, children = stop_case(queue, tmp_path, evidence, monkeypatch, 'wall')
    records, actions = attempts(owner), []
    current = owner._current

    def observation():
        if len(records) == 1:
            # Independent fixture actor ends only this toy; the owner did not
            # receive an accepted manager action or its stop witness.
            os.kill(owner.foreground.identity['pid'], signal.SIGTERM)
            assert os.waitid(os.P_PID, owner.foreground.identity['pid'],
                             os.WEXITED | os.WNOWAIT).si_pid == owner.foreground.identity['pid']
            raise OSError('synthetic reply lost before independently observed end')
        return current()

    owner._current = observation
    owner._control = lambda *args: actions.append(args)
    try:
        result = owner.run(snapshot)
        assert result is not None and result['foreground']['returncode'] == -signal.SIGTERM
        assert len(records) == 2 and actions == [] and result['stop'] is None
        state = queue.execution_status(claimed['context'])
        assert state['first_stop'] is None and state['control_closure']['data'] == result
    finally:
        reap(children)


def test_accepted_action_with_failed_publication_is_not_sent_twice(
        queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, claimed, children = stop_case(queue, tmp_path, evidence, monkeypatch, 'wall')
    records, actions, publications = attempts(owner), [], []
    publish = queue.remember_native_event

    def remember(context, kind, data, **kwargs):
        if kind == 'stop':
            publications.append(data)
            raise sqlite3.OperationalError('synthetic stop publication failure')
        return publish(context, kind, data, **kwargs)

    def control(action, unit, remaining):
        actions.append(action)
        owner.foreground.terminate()

    monkeypatch.setattr(queue, 'remember_native_event', remember)
    owner._control = control
    try:
        result = owner.run(snapshot)
        assert result is not None and result['stop']['reason'] == 'wall'
        assert len(records) == len(publications) == 1 and actions == ['stop']
        assert publications[0] == result['stop'] and 'OperationalError' in owner.diagnostics
        state = queue.execution_status(claimed['context'])
        assert state['control_closure']['data'] == result
        assert state['first_stop']['data'] == result['stop'], 'closure must retain the accepted fact'
    finally:
        reap(children)


@pytest.mark.parametrize('reason', ['wall', 'cancellation', 'pressure_stop'])
def test_persistent_stop_refusal_exhausts_original_budget_with_open_control(
        queue, tmp_path, evidence, monkeypatch, reason):
    owner, snapshot, claimed, children = stop_case(queue, tmp_path, evidence, monkeypatch, reason)
    records = attempts(owner)
    actions = []

    def refused():
        raise ValueError('synthetic original identity cannot be observed')

    owner._current = refused
    owner._control = lambda *args: actions.append(args)
    try:
        assert owner.run(snapshot) is None
        assert len(records) >= 2, 'one failed observation abandoned the original draining loop'
        assert {r['deadline'] for r in records} == {owner.final_deadline}
        assert {r['reason'] for r in records} == {reason}
        assert time.monotonic() >= owner.final_deadline
        assert actions == [] and owner.foreground.status() is None
        state = queue.execution_status(claimed['context'])
        assert state['first_stop'] is state['control_closure'] is state['native_intent'] is None
    finally:
        reap(children)
