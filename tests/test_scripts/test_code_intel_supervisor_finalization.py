"""Actual Supervisor/SQL finalization; controller locks are synthetic in this unit fixture.

The separate sealed orphan CLI suite exercises the real captured lock boundary.
Here a real ended harmless child supplies owner identity and the real facade
executes drain/commit/retirement; only the coordination callbacks are replaced.
"""

import time
from contextlib import nullcontext
from copy import deepcopy

import pytest

from tests.test_scripts.test_code_intel_orphan_cli import setup
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def direct(tmp_path, monkeypatch):
    _, _, snapshot, modules, claimed = setup(tmp_path, monkeypatch, 'runner')
    monkeypatch.setattr(modules['owner_admission'], 'repository_lock', lambda *args: nullcontext())
    guarded = []
    supervisor = modules['supervisor'].Supervisor(snapshot, modules, lambda: guarded.append(True))
    return supervisor, modules, claimed, guarded


def publish_after_reconciliation(modules, monkeypatch, *, conflict):
    queue = modules['queue']
    original = modules['supervisor'].reconcile_orphan
    paths = []

    def reconcile(*args, **kwargs):
        result = original(*args, **kwargs)
        data = deepcopy(result['control_closure']['data'])
        if conflict:
            data['observed_ns'] += 1
        event = queue._native.event(args[1], 'control-closure', data, time.time(), queue._proof)
        paths.append(queue._spool_native_event(event))
        if conflict:
            data['observed_ns'] += 1
            paths.append(queue._spool_native_event(queue._native.event(
                args[1], 'control-closure', data, time.time(), queue._proof)))
        else:
            paths.append(queue._spool_native_event({**event, 'recorded_at': event['recorded_at'] + 1}))
        return result

    monkeypatch.setattr(modules['supervisor'], 'reconcile_orphan', reconcile)
    return paths, original


def test_supervisor_final_drain_retains_callback_conflict_without_grant(tmp_path, monkeypatch):
    supervisor, modules, claimed, guarded = direct(tmp_path, monkeypatch)
    queue = modules['queue']
    paths, _ = publish_after_reconciliation(modules, monkeypatch, conflict=True)
    result = supervisor.reconcile_context(claimed['context'])
    assert guarded == [True]
    assert result['first_typed_conflict'] is not None
    assert result['control_closure']['data']['state'] == 'orphaned'
    assert result['launch_permission'] == 'cancelled' and result['typed_charged_attempts'] == 0
    assert len(paths) == 2 and sum(path.is_file() for path in paths) == 1
    assert queue.execution_status(claimed['context'])['evidence_pending'] is True
    with pytest.raises(RuntimeError, match='unrepresented'):
        queue.claim_native(claimed['hash'], provider='cbm')
    assert sum(path.is_file() for path in paths) == 1


def test_supervisor_callback_failure_rolls_back_but_keeps_durable_evidence(tmp_path, monkeypatch):
    supervisor, modules, claimed, _ = direct(tmp_path, monkeypatch)
    queue = modules['queue']
    before = queue.execution_status(claimed['context'])
    paths, reconcile = publish_after_reconciliation(modules, monkeypatch, conflict=False)
    wrapped = modules['supervisor'].reconcile_orphan

    def fail(*args, **kwargs):
        wrapped(*args, **kwargs)
        raise OSError('injected callback failure after durable publication')

    with monkeypatch.context() as patch:
        patch.setattr(modules['supervisor'], 'reconcile_orphan', fail)
        with pytest.raises(OSError, match='callback failure'):
            supervisor.reconcile_context(claimed['context'])
    observed = queue.execution_status(claimed['context'])
    assert observed.pop('evidence_pending') is True
    before.pop('evidence_pending')
    assert observed == before and len(paths) == 2 and all(path.is_file() for path in paths)
    monkeypatch.setattr(modules['supervisor'], 'reconcile_orphan', reconcile)
    result = supervisor.reconcile_context(claimed['context'])
    assert result['typed_charged_attempts'] == 0
    assert queue.get_inflight(claimed['hash']) is None
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize('partial', [False, True])
def test_supervisor_retirement_failure_replays_committed_row_without_repricing(
    tmp_path, monkeypatch, partial
):
    supervisor, modules, claimed, _ = direct(tmp_path, monkeypatch)
    queue = modules['queue']
    paths, reconcile = publish_after_reconciliation(modules, monkeypatch, conflict=False)
    original = queue._legacy.retire

    def fail(directory, imported, fsync):
        if imported:
            assert len(imported) == 2
            if partial:
                original(directory, imported[:1], fsync)
            raise OSError('injected retirement failure after commit')
        return original(directory, imported, fsync)

    with monkeypatch.context() as patch:
        patch.setattr(queue._legacy, 'retire', fail)
        with pytest.raises(OSError, match='retirement failure'):
            supervisor.reconcile_context(claimed['context'])
    committed = queue.execution_status(claimed['context'])
    assert committed.pop('evidence_pending') is True
    assert committed['control_closure']['data']['state'] == 'orphaned'
    assert committed['typed_charged_attempts'] == 0
    assert queue.get_inflight(claimed['hash']) is None
    assert sum(path.exists() for path in paths) == (1 if partial else 2)
    monkeypatch.setattr(modules['supervisor'], 'reconcile_orphan', reconcile)
    replay = supervisor.reconcile_context(claimed['context'])
    assert replay == committed
    assert not any(path.exists() for path in paths)
    assert queue.claim_native(claimed['hash'], provider='cbm') is None
