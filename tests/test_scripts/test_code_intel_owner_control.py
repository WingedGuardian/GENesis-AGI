"""Actual toy lifetime plus scratch journal: needed physical action precedes SQL."""
import runpy
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_owned_child import toy as toy
from tests.test_scripts.test_index_marker_control import _reserved
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _registration


def prepared(queue, tmp_path, toy, evidence):
    path = Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_owner.py'
    namespace = runpy.run_path(str(path))
    constructor = namespace.get('PhysicalOwner')
    assert callable(constructor), 'physical control must remain with the original live owner'
    claimed, reserved = _reserved(queue, tmp_path)
    proof = SimpleNamespace(**evidence)
    facts = _registration(claimed)['facts']
    subject = namespace['OwnedChild'](toy, proof)
    facts['reporter'] = subject.identity
    calls = []
    manager = SimpleNamespace(deadline=time.monotonic() + reserved['wall_seconds'])
    manager._object = lambda *args: '/org/freedesktop/systemd1/unit/owned'
    manager.registration = lambda unit, current, **kwargs: dict(
        unit=unit, invocation_id='1' * 32, facts=deepcopy(current), stop_usec=90_000_000)
    probe = SimpleNamespace(capture=lambda **kwargs: deepcopy(facts))

    def control(action, unit, remaining):
        assert unit == reserved['unit'] and remaining > 0
        calls.append(action)
        if action == 'stop':
            subject.terminate()

    modules = dict(queue=queue, proof=proof, queued=SimpleNamespace(**runpy.run_path(
        str(path.with_name('code_intel_queued_worker.py')))), unit=None, scope=None)
    owner = constructor(modules, claimed['context'], reserved, probe, manager, control=control)
    owner.foreground = subject
    return owner, calls, facts, manager, claimed


def test_stop_is_physically_accepted_before_potentially_blocking_publication(queue, tmp_path, toy, evidence, monkeypatch):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    owner.early()
    original = queue.remember_native_event
    def publish(context, kind, data):
        assert calls == ['stop']
        assert toy.stdout.read() == b'', 'the toy received actual termination before journal entry'
        calls.append('journal')
        return original(context, kind, data)
    monkeypatch.setattr(queue, 'remember_native_event', publish)
    owner.control('stop', reason='pressure_stop')
    assert calls == ['stop', 'journal']
    assert queue.execution_status(claimed['context'])['safety_kind'] == 'resource'


def test_pause_and_thaw_use_verified_original_but_do_not_publish_stop(queue, tmp_path, toy, evidence):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    owner.early()
    owner.control('freeze')
    owner.control('thaw')
    assert calls == ['freeze', 'thaw']
    assert queue.execution_status(claimed['context'])['first_stop'] is None


@pytest.mark.parametrize('fault', ['invocation', 'object-path', 'leaf'])
def test_foreign_lifetime_gets_neither_signal_nor_stop_event(queue, tmp_path, toy, evidence, fault):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    owner.early()
    original = manager.registration
    if fault == 'invocation':
        manager.registration = lambda *args, **kwargs: {**original(*args, **kwargs), 'invocation_id': '2' * 32}
    elif fault == 'object-path':
        manager._object = lambda *args: '/org/freedesktop/systemd1/unit/foreign'
    else:
        facts['leaf']['inode'] += 1
    with pytest.raises(ValueError):
        owner.control('stop', reason='cancellation')
    assert not calls and queue.execution_status(claimed['context'])['first_stop'] is None


@pytest.mark.parametrize('counter', ['events', 'local_events'])
def test_first_early_oom_is_retained_but_cannot_release_startup(queue, tmp_path, toy, evidence, counter):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    facts[counter]['oom_kill'] = 1
    with pytest.raises(ValueError, match='owned OOM'):
        owner.early()
    assert owner.startup['facts'][counter]['oom_kill'] == 1
    assert not calls and queue.execution_status(claimed['context'])['scope_registration'] is None


@pytest.mark.parametrize('fault', ['counter-reset', 'replaced-leaf', 'later-counter-reset'])
@pytest.mark.parametrize('counter', ['events', 'local_events'])
def test_first_kernel_oom_cannot_be_replaced_after_manager_read_failure(queue, tmp_path, toy, evidence, fault, counter):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    original = manager.registration
    facts[counter]['oom_kill'] = 0 if fault == 'later-counter-reset' else 1

    def unavailable(*args, **kwargs):
        raise ValueError('synthetic manager read failure after kernel sample')

    manager.registration = unavailable
    with pytest.raises(ValueError):
        owner.early()
    if fault == 'later-counter-reset':
        facts[counter]['oom_kill'] = 1
        with pytest.raises(ValueError):
            owner.early()
    manager.registration = original
    facts[counter]['oom_kill'] = 0
    if fault == 'replaced-leaf':
        facts['leaf']['inode'] += 1
    with pytest.raises(ValueError):
        owner.early()
    assert not calls and queue.execution_status(claimed['context'])['native_intent'] is None


def test_manager_retry_keeps_first_baseline_and_current_non_oom_diagnostics(queue, tmp_path, toy, evidence):
    owner, _, facts, manager, _ = prepared(queue, tmp_path, toy, evidence)
    original = manager.registration
    manager.registration = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('synthetic manager read failure'))
    with pytest.raises(ValueError):
        owner.early()
    manager.registration = original
    facts['events']['high'] += 1
    value = owner.early()
    assert value['scope']['facts']['events']['high'] == 0
    assert owner.startup['facts']['events']['high'] == 1


def test_fixed_final_budget_is_not_renewed_and_stop_survives_lost_publication(queue, tmp_path, toy, evidence, monkeypatch):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    owner.early()
    def lost(*args, **kwargs):
        raise OSError('synthetic evidence I/O failure')
    monkeypatch.setattr(queue, 'remember_native_event', lost)
    owner.control('stop', reason='wall')
    deadline = owner.final_deadline
    assert calls == ['stop'] and owner.stop['reason'] == 'wall'
    assert owner.diagnostics
    owner.clock = lambda: deadline + 1
    with pytest.raises(TimeoutError, match='deadline'):
        owner.control('stop', reason='wall')
    assert calls == ['stop'] and owner.final_deadline == deadline


@pytest.mark.parametrize('counter', ['events', 'local_events'])
def test_quiescent_cleanup_retains_new_owned_oom_before_declining_an_unneeded_signal(queue, tmp_path, toy, evidence, counter):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    owner.early()
    owner.foreground.terminate()
    toy.stdout.read()
    while owner.foreground.status() is None:
        time.sleep(.001)
    facts[counter]['oom_kill'] = 1
    owner.modules['scope'] = SimpleNamespace(cleanup_observation=lambda *args, **kwargs: dict(
        state='quiescent', facts=deepcopy(facts), populated=0, owned_wait=True))
    assert owner.control('stop', reason='cancellation') is False
    assert owner.startup['facts'][counter]['oom_kill'] == 1
    assert not calls and queue.execution_status(claimed['context'])['first_stop'] is None


@pytest.mark.parametrize('counter', ['events', 'local_events'])
def test_valid_original_kernel_oom_survives_later_manager_refusal(queue, tmp_path, toy, evidence, counter):
    owner, calls, facts, manager, claimed = prepared(queue, tmp_path, toy, evidence)
    owner.early()
    facts[counter]['oom_kill'] = 1
    original = manager.registration
    manager.registration = lambda *args, **kwargs: {**original(*args, **kwargs), 'invocation_id': '2' * 32}
    with pytest.raises(ValueError):
        owner.control('stop', reason='cancellation')
    assert owner.startup['facts'][counter]['oom_kill'] == 1
    assert not calls
