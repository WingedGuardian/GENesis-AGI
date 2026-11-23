"""Retained raw hazards remain present and covered through ordinary launch."""

import time
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import (
    _finish_control,
    _intent,
    _registration,
    _reservation,
)
from tests.test_scripts.test_index_marker_native_replay import _cleanup, _terminal
from tests.test_scripts.test_index_marker_retry_acceptance import accept
from tests.test_scripts.test_index_marker_retry_consumption import issued
from tests.test_scripts.test_index_marker_retry_grant import candidate


def residue_trial(queue, tmp_path):
    repo = str(tmp_path / 'repo')
    queue.write_marker(repo, 'both', 'full')
    old = queue.claim_native(queue.marker_hash(repo), provider='cbm')
    assert queue.reserve_launch(old['context'], _reservation(old)) == 'reserved'
    assert queue.register_scope(old['context'], _registration(old)) == 'registered'
    assert queue.begin_native(old['context'], _intent(queue, old)) == 'started'
    queue.remember_native_event(old['context'], 'terminal', _terminal(old, rc=1))
    queue.remember_native_event(old['context'], 'cleanup', _cleanup(queue, old))
    _finish_control(queue, old)
    assert queue.apply_native(old['context']) == 'pending'
    with pytest.raises(RuntimeError, match='conflict'):
        queue.remember_native_event(old['context'], 'terminal', _terminal(old, rc=3))
    extra = queue._native.event(old['context'], 'terminal', _terminal(old, rc=75), 1000, queue._proof)
    path = queue._spool_native_event(extra)
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match='unrepresented'):
        queue.claim_native(old['hash'], provider='cbm')
    retry, grant, request = issued(queue, tmp_path, debt=1)
    with queue._decision_gate(native=True) as (db, finalize):
        trial, _ = retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
                                 bind_runtime=lambda context, reserved: object())
    queue.register_scope(trial['context'], _registration(trial))
    queue.begin_native(trial['context'], _intent(queue, trial))
    queue.remember_native_event(trial['context'], 'terminal', _terminal(trial))
    queue.remember_native_event(trial['context'], 'cleanup', _cleanup(queue, trial))
    _finish_control(queue, trial)
    assert queue.apply_native(trial['context']) == 'consumed'
    return retry, grant, trial, old, path, raw


@pytest.mark.parametrize('replace_grant', [False, True])
def test_accepted_older_raw_residue_allows_claim_reserve_register_begin(queue, tmp_path, replace_grant):
    retry, grant, trial, old, path, raw = residue_trial(queue, tmp_path)
    assert accept(queue, retry, grant)['state'] == 'accepted'
    if replace_grant:
        # Use the actual grant lifecycle; no direct metadata rewrite or deletion.
        data = {**candidate(queue, tmp_path), 'nonce': 'c' * 32,
                'not_after_wall_ns': time.time_ns() + 3600 * 10**9}
        with queue._decision_gate(native=True) as (db, finalize):
            replacement = retry.issue(db, data, queue._native, queue._legs, finalize=finalize)
        assert replacement['status'] == 'issued'
    queue.write_marker(grant['repo_path'], 'cbm', 'fast')
    next_run = queue.claim_native(grant['hash'], provider='cbm')
    assert next_run is not None
    assert queue.reserve_launch(next_run['context'], _reservation(next_run)) == 'reserved'
    assert queue.register_scope(next_run['context'], _registration(next_run)) == 'registered'
    assert queue.begin_native(next_run['context'], _intent(queue, next_run)) == 'started'
    assert path.read_bytes() == raw
    with closing(queue._connect()) as db:
        assert db.execute('SELECT safety_kind FROM executions WHERE execution_seq=?',
                          (old['context']['execution_seq'],)).fetchone()[0] == 'conflict'
        assert db.execute('SELECT accepted_sequence FROM provider_state WHERE hash=? AND provider=?',
                          (grant['hash'], 'cbm')).fetchone()[0] == trial['context']['execution_seq']


@pytest.mark.parametrize('fault', ['missing', 'waiting', 'trial', 'foreign', 'malformed', 'unbound'])
def test_ordinary_residual_reader_requires_valid_exact_older_annotation(queue, tmp_path, fault):
    retry, grant, trial, _, _, _ = residue_trial(queue, tmp_path)
    accept(queue, retry, grant)
    with queue._decision_gate(native=True) as (db, finalize):
        annotations = {}
        residual = finalize(residual_context=annotations)
        assert len(residual) == 1 and len(residual[0]) == 4
        key = residual[0]
        context, disposition = annotations[key]
        if fault == 'missing':
            annotations.clear()
        elif fault == 'waiting':
            annotations[key] = context, 'waiting'
        elif fault == 'trial':
            annotations[key] = trial['context'], disposition
        elif fault == 'foreign':
            annotations[key] = {**context, 'provider': 'gitnexus'}, disposition
        elif fault == 'malformed':
            annotations[key] = {'execution_seq': 1}, disposition
        else:
            db.execute("UPDATE executions SET completion_kind='unbound' WHERE execution_seq=?",
                       (context['execution_seq'],))
        with pytest.raises(RuntimeError, match='unrepresented'):
            queue._recovery.refuse_unrepresented(db, residual, (grant['hash'], 'cbm'), residual_context=annotations)
        # An unrelated provider is not held by this provider's extra terminal.
        queue._recovery.refuse_unrepresented(db, residual, (grant['hash'], 'gitnexus'), residual_context=annotations)


def test_final_drain_replaces_annotation_map_instead_of_reusing_caller_entries(queue, tmp_path):
    _, _, _, old, _, _ = residue_trial(queue, tmp_path)
    with queue._decision_gate(native=True) as (_, finalize):
        annotations = {('invented',): (old['context'], 'retained')}
        first = finalize(residual_context=annotations)
        assert set(annotations) == set(first)
        annotations[('invented',)] = old['context'], 'retained'
        assert finalize(residual_context=annotations) == first
        assert set(annotations) == set(first)


@pytest.mark.parametrize('subject', ['older', 'trial'])
def test_actual_spool_published_by_last_callback_is_decided_at_exact_cutoff(queue, tmp_path, subject):
    retry, grant, trial, old, path, raw = residue_trial(queue, tmp_path)
    target = old if subject == 'older' else trial
    published = []
    def observe():
        event = queue._native.event(target['context'], 'terminal', _terminal(target, rc=42), 1000, queue._proof)
        published.append(queue._spool_native_event(event))
        return dict(source=grant['source'], policy_sha256=grant['policy_sha256'])
    if subject == 'older':
        assert accept(queue, retry, grant, observe=observe)['state'] == 'accepted'
    else:
        with pytest.raises(ValueError, match='newer hazard'):
            accept(queue, retry, grant, observe=observe)
        with closing(queue._connect()) as db:
            assert db.execute('SELECT accepted_sequence FROM provider_state WHERE hash=? AND provider=?',
                              (grant['hash'], 'cbm')).fetchone()[0] == 0
    assert path.read_bytes() == raw and published[0].exists()


def test_later_resource_failure_is_not_covered_by_old_accepted_receipt(queue, tmp_path):
    retry, grant, trial, _, path, raw = residue_trial(queue, tmp_path)
    accept(queue, retry, grant)
    queue.write_marker(grant['repo_path'], 'cbm', 'full')
    later = queue.claim_native(grant['hash'], provider='cbm')
    assert later['context']['execution_seq'] > trial['context']['execution_seq']
    queue.reserve_launch(later['context'], _reservation(later))
    queue.register_scope(later['context'], _registration(later))
    queue.begin_native(later['context'], _intent(queue, later))
    queue.remember_native_event(later['context'], 'terminal', _terminal(later, rc=1, oom=1))
    queue.remember_native_event(later['context'], 'cleanup', _cleanup(queue, later, oom=1))
    _finish_control(queue, later)
    assert queue.apply_native(later['context']) == 'pending'
    queue.write_marker(grant['repo_path'], 'cbm', 'full')
    assert queue.claim_native(grant['hash'], provider='cbm') is None
    assert path.read_bytes() == raw
    with closing(queue._connect()) as db:
        assert db.execute('SELECT accepted_sequence FROM provider_state WHERE hash=? AND provider=?',
                          (grant['hash'], 'cbm')).fetchone()[0] == trial['context']['execution_seq']
        assert queue._recovery.provider_blocked(db, grant['hash'], 'cbm', queue._native)


def test_older_population_published_by_last_callback_blocks_acceptance(queue, tmp_path):
    retry, grant, _, old, path, raw = residue_trial(queue, tmp_path)
    published = []
    def observe():
        data = {**_cleanup(queue, old), 'populated': 1, 'observed_ns': 400}
        event = queue._native.event(old['context'], 'cleanup-disproof', data, 1000, queue._proof)
        published.append(queue._spool_native_event(event))
        return dict(source=grant['source'], policy_sha256=grant['policy_sha256'])
    with pytest.raises((ValueError, RuntimeError)):
        accept(queue, retry, grant, observe=observe)
    assert path.read_bytes() == raw and published[0].exists()
    with closing(queue._connect()) as db:
        assert db.execute('SELECT accepted_sequence FROM provider_state WHERE hash=? AND provider=?',
                          (grant['hash'], 'cbm')).fetchone()[0] == 0


@pytest.mark.parametrize('late_population', [False, True])
def test_fresh_proof_of_sticky_history_is_reused_only_for_unchanged_physical_facts(queue, tmp_path, late_population):
    retry, grant, _, old, path, raw = residue_trial(queue, tmp_path)
    def population(observed_ns):
        data = {**_cleanup(queue, old), 'populated': 1, 'observed_ns': observed_ns}
        return queue._spool_native_event(queue._native.event(old['context'], 'cleanup-disproof', data, 1000, queue._proof))
    population(400)
    calls = []
    after_observation = False
    def fresh(row):
        assert not after_observation, 'no physical callback after final metadata observation'
        assert row['execution_seq'] == old['context']['execution_seq']
        calls.append(row['execution_seq'])
        return True  # Synthetic original-subtree proof; kernel qualification is separate.
    def observe():
        nonlocal after_observation
        after_observation = True
        if late_population:
            population(500)
        return dict(source=grant['source'], policy_sha256=grant['policy_sha256'])
    if late_population:
        with pytest.raises(RuntimeError, match='final physical'):
            accept(queue, retry, grant, observe=observe, fresh=fresh)
    else:
        assert accept(queue, retry, grant, observe=observe, fresh=fresh)['state'] == 'accepted'
    assert calls and path.read_bytes() == raw


def test_late_sibling_cleanup_disproof_remains_repository_wide_exclusion(queue, tmp_path):
    retry, grant, _, _, _, _ = residue_trial(queue, tmp_path)
    sibling = queue.claim_native(grant['hash'], provider='gitnexus')
    queue.reserve_launch(sibling['context'], _reservation(sibling))
    queue.register_scope(sibling['context'], _registration(sibling))
    queue.begin_native(sibling['context'], _intent(queue, sibling))
    queue.remember_native_event(sibling['context'], 'terminal', _terminal(sibling))
    queue.remember_native_event(sibling['context'], 'cleanup', _cleanup(queue, sibling))
    _finish_control(queue, sibling)
    assert queue.apply_native(sibling['context']) == 'consumed'
    def observe():
        data = {**_cleanup(queue, sibling), 'populated': 1, 'observed_ns': 400}
        queue._spool_native_event(queue._native.event(sibling['context'], 'cleanup-disproof', data, 1000, queue._proof))
        return dict(source=grant['source'], policy_sha256=grant['policy_sha256'])
    with pytest.raises(RuntimeError, match='final physical'):
        accept(queue, retry, grant, observe=observe)
