"""Real harmless children and scratch SQL; no provider or native scope launch."""
import os
import shlex
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_owner_commands import prepared as command_case
from tests.test_scripts.test_code_intel_owner_loop import observed_children, prepared
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


def retire(children):
    for child in children:
        if child.poll() is None:
            child.kill()  # Only actual harmless fixture children, after assertions.
        child.wait(timeout=5)


def test_cancel_before_header_has_a_local_wait_bound(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, payload, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    children = observed_children(namespace, monkeypatch)
    armed = tmp_path / 'term-handler-armed'
    program = ('import os,pathlib,select,signal,sys,time;'
        'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        'pathlib.Path(sys.argv[2]).write_text("armed");fd=int(sys.argv[1]);end=time.monotonic()+8;'
        '\nwhile time.monotonic()<end:\n'
        ' if select.select([fd],[],[],.02)[0] and not os.read(fd,4096): break\n')
    monkeypatch.setitem(namespace, 'scope_command', lambda snap, mods, fd:
        [sys.executable, '-I', '-c', program, str(fd), str(armed)])

    def unavailable(*args):
        until = time.monotonic() + 5
        while not armed.exists() and time.monotonic() < until:
            time.sleep(.005)
        assert armed.exists(), 'TERM ignore must be installed before cancellation'
        raise ValueError('synthetic unavailable startup header')

    owner.manager._object = unavailable
    owner._control = lambda *args: pytest.fail('unobserved scope action')
    owner.cancelled.append(signal.SIGTERM)
    owner.work_deadline = time.monotonic() + 4
    began = time.monotonic()
    try:
        assert owner.run(snapshot) is None
        assert time.monotonic() - began < 3, 'cancellation waited for work deadline'
        assert owner.startup is None and owner.final_deadline is None
        assert not payload.exists()
        state = queue.execution_status(claimed['context'])
        assert state['native_intent'] is state['control_closure'] is None
    finally:
        retire(children)


def test_refused_preheader_term_does_not_renew_first_local_bound(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, payload, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    children = observed_children(namespace, monkeypatch)
    now, waits, attempts = [time.monotonic()], [], []
    owner.clock = lambda: now[0]
    owner.work_deadline = now[0] + 100
    owner.cancelled.append(signal.SIGTERM)
    owner.manager._object = lambda *args: (_ for _ in ()).throw(ValueError('no header'))

    def refused(child, **kwargs):
        attempts.append(now[0])
        raise OSError('synthetic refusal on this actual child')

    def advance(*args):
        waits.append(now[0])
        assert len(waits) < 40, 'repeated stop requests renewed their deadline'
        now[0] += .2
        return [], [], []

    monkeypatch.setattr(namespace['OwnedChild'], 'terminate', refused)
    monkeypatch.setitem(namespace, 'select', SimpleNamespace(select=advance))
    began = now[0]
    try:
        assert owner.run(snapshot) is None
        assert attempts and 2 <= now[0] - began < 3
        assert owner.startup is None and owner.final_deadline is None and not payload.exists()
        assert queue.execution_status(claimed['context'])['control_closure'] is None
    finally:
        retire(children)


def test_preheader_late_group_absence_cannot_close_after_local_bound(queue, tmp_path, evidence, monkeypatch):
    owner, snapshot, payload, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    children = observed_children(namespace, monkeypatch)
    now, absence = [time.monotonic()], []
    owner.clock = lambda: now[0]
    owner.work_deadline = now[0] + 10
    owner.cancelled.append(signal.SIGTERM)
    owner.manager._object = lambda *args: (_ for _ in ()).throw(ValueError('no header'))
    monkeypatch.setitem(namespace, 'scope_command', lambda *args: [sys.executable, '-I', '-c', 'pass'])

    def absent(pid, sig):
        assert sig == 0
        absence.append(pid)
        now[0] += 3
        raise ProcessLookupError('late original monitor group absence')

    monkeypatch.setitem(namespace, 'os', SimpleNamespace(**{**vars(os), 'killpg': absent}))
    try:
        assert owner.run(snapshot) is None, 'late local proof invented closure'
        assert absence and owner.final_deadline is None and not payload.exists()
        assert queue.execution_status(claimed['context'])['control_closure'] is None
    finally:
        retire(children)


@pytest.mark.parametrize('failure', ['transient', 'permanent', 'after-success', 'structural', 'retained-facts'])
def test_cleanup_requires_a_fresh_read_inside_the_original_budget(
        queue, tmp_path, evidence, monkeypatch, failure):
    owner, snapshot, payload, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    children = observed_children(namespace, monkeypatch)
    observation = owner.modules['scope'].cleanup_observation
    registration = owner.manager.registration
    owner.manager.registration = lambda *args, **kwargs: {**registration(*args, **kwargs), 'stop_usec': 200_000}
    calls, drained = [], []
    retain = owner._retain_facts

    def cleanup(*args, **kwargs):
        calls.append(True)
        if failure == 'structural':
            raise ValueError('synthetic changed original identity')
        if (failure == 'permanent' or failure == 'transient' and len(calls) == 1
                or failure == 'after-success' and len(calls) > 1):
            raise FileNotFoundError('synthetic scope-file disappearance during cleanup')
        return observation(*args, **kwargs)

    def retained(facts):
        if failure == 'retained-facts' and calls:
            raise OSError('retained facts publication is not a transient cleanup read')
        return retain(facts)

    def clients():
        drained.append(True)
        return not (failure == 'after-success' and len(drained) == 1)

    owner.modules['scope'].cleanup_observation = cleanup
    owner._retain_facts = retained
    owner._control_clients_drained = clients
    send = owner.modules['queued'].StartupGate.send

    def acknowledged(gate, fd, ack):
        assert queue.register_scope(claimed['context'], ack['scope']) == 'registered'
        send(gate, fd, ack)

    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', acknowledged)
    try:
        result = owner.run(snapshot)
        state = queue.execution_status(claimed['context'])
        assert payload.read_text() == 'entered'
        if failure == 'transient':
            assert result is not None, ('one cleanup read abandoned the drain', owner.diagnostics)
            assert len(calls) >= 2
            assert state['control_closure']['data'] == result
            assert state['cleanup_proof']['data']['state'] == 'quiescent'
        else:
            assert result is None and state['control_closure'] is state['cleanup_proof'] is None
            if failure in ('structural', 'retained-facts'):
                # Original failure handling makes one last identity-bound stop
                # observation; it does not enter the transient drain retry.
                assert len(calls) == 2
            else:
                assert len(calls) >= 2 and time.monotonic() >= owner.final_deadline
    finally:
        retire(children)


def test_initial_scope_requests_the_frozen_independent_wall(tmp_path, monkeypatch):
    snapshot, modules, value, owner = command_case(tmp_path, monkeypatch)
    monkeypatch.setattr(modules['systemd'], 'literal_scope_args', lambda **kwargs: [])
    tokens = shlex.split(owner['scope_command'](snapshot, modules, 7)[5])
    assert 'RuntimeMaxSec=' + str(value['policy']['wall_seconds']) in tokens
    assert 'RuntimeRandomizedExtraSec=0' in tokens
    assert 'FinalKillSignal=SIGKILL' in tokens


@pytest.mark.parametrize('fault', [None, 'runtime', 'random', 'signal'])
def test_effective_runtime_refusal_precedes_actual_owner_ack(queue, tmp_path, evidence, monkeypatch, fault):
    from pathlib import Path

    from tests.test_scripts.test_code_intel_scope_manager import UNIT, Bus, manager, variant
    from tests.test_scripts.test_code_intel_scope_runtime import runtime_bus
    from tests.test_scripts.test_index_marker_execution import _registration

    owner, snapshot, payload, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    children = observed_children(namespace, monkeypatch)
    facts = _registration(claimed)['facts']

    class OwnerBus(Bus):
        def __call__(self, path, interface, method, *args, timeout):
            if method == 'GetUnit':
                assert args == ('s', owner.reserved['unit'] + '.scope')
                args = ('s', UNIT + '.scope')
            elif method == 'GetUnitByPID':
                assert args == ('u', str(owner.foreground.identity['pid']))
                args = ('u', '123')
            return super().__call__(path, interface, method, *args, timeout=min(timeout, 1 - self.now))

    bus = OwnerBus()
    bus.scope.update(runtime_bus(owner.reserved['wall_seconds']).scope)
    bus.unit['Id'] = variant('s', owner.reserved['unit'] + '.scope')
    bus.scope.update(ControlGroup=variant('s', '/visible/' + Path(facts['leaf']['path']).parent.name
        + '/' + owner.reserved['unit'] + '.scope'), ControlGroupId=variant('t', facts['leaf']['inode']))
    if fault is not None:
        name, value = {'runtime': ('RuntimeMaxUSec', variant('t', 2**64 - 1)),
            'random': ('RuntimeRandomizedExtraUSec', variant('t', 1)),
            'signal': ('FinalKillSignal', variant('i', 15))}[fault]
        bus.scope[name] = value
    generic = manager(bus)
    owner.manager = type(generic)(bus, generic.property, deadline=1, clock=lambda: bus.now,
                                  require_runtime_limit=True)
    owner.work_deadline = time.monotonic() + (10 if fault is None else .4)
    try:
        result = owner.run(snapshot)
        state = queue.execution_status(claimed['context'])
        assert payload.exists() is (fault is None), ('effective runtime gate did not guard ACK', owner.diagnostics)
        if fault is None:
            assert result is not None and owner.startup is not None
        else:
            assert result is None and owner.startup is None and owner.final_deadline is None
            assert state['native_intent'] is state['control_closure'] is None
        assert any(call[3] == ('s', 'org.freedesktop.systemd1.Scope') for call in bus.calls)
    finally:
        retire(children)
