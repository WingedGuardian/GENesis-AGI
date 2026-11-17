"""Acceptance closes after its final metadata callback, retaining raw evidence."""

import time
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_accepted_residual import residue_trial
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_native_replay import _cleanup
from tests.test_scripts.test_index_marker_retry_acceptance import accept, receipt


@pytest.mark.parametrize('subject', ['older', 'trial'])
@pytest.mark.parametrize('late', [False, True])
def test_full_conflict_slots_cannot_hide_disproof_at_acceptance_cutoff(queue, tmp_path, subject, late):
    retry, grant, trial, old, original, raw = residue_trial(queue, tmp_path)
    # Fill both conflict slots before this decision; later distinct evidence
    # must remain visible even when it cannot replace those immutable slots.
    for tick in (400, 500):
        data = {**_cleanup(queue, old), 'populated': 1, 'observed_ns': tick}
        if tick == 400:
            queue.remember_native_event(old['context'], 'cleanup-disproof', data)
        else:
            with pytest.raises(RuntimeError, match='conflict'):
                queue.remember_native_event(old['context'], 'cleanup-disproof', data)
    before = receipt(queue, grant)
    calls, published = [], []

    def fresh(row):
        # This explicitly tests the guard's decision-local callback contract.
        # It is not a claim about the real collector or native kernel state.
        assert row['execution_seq'] == old['context']['execution_seq']
        calls.append(row['execution_seq'])
        return True

    def observe():
        if late:
            target = old if subject == 'older' else trial
            data = {**_cleanup(queue, target), 'populated': 1, 'observed_ns': 600}
            item = queue._native.event(target['context'], 'cleanup-disproof', data,
                                       time.time(), queue._proof)
            published.append(queue._spool_native_event(item))
        return dict(source=grant['source'], policy_sha256=grant['policy_sha256'])

    if late:
        with pytest.raises(RuntimeError, match='physical'):
            accept(queue, retry, grant, observe=observe, fresh=fresh)
        assert receipt(queue, grant) == before
        assert len(published) == 1 and published[0].is_file()
    else:
        assert accept(queue, retry, grant, observe=observe, fresh=fresh)['state'] == 'accepted'
    assert calls and original.read_bytes() == raw
    with closing(queue._connect()) as db:
        older = queue._native.execution(db, old['context'], queue._legs)
        assert older['first_typed_conflict'] and older['cleanup_first_failure']
        assert older['cleanup_invalidated'] == 1


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
@pytest.mark.parametrize('fault', ['population', 'resource'])
def test_late_trial_evidence_reholds_ordinary_admission_without_changing_receipt(queue, tmp_path, provider, fault):
    from tests.test_scripts.test_index_marker_native_replay import _terminal
    from tests.test_scripts.test_index_marker_retry_acceptance import completed

    retry, grant, trial, _ = completed(queue, tmp_path, provider)
    accept(queue, retry, grant)
    before = receipt(queue, grant)
    kind, data = ('cleanup-disproof', {**_cleanup(queue, trial), 'populated': 1}) if fault == 'population' else (
        'terminal', _terminal(trial, rc=1, oom=1))
    if fault == 'resource':
        with pytest.raises(RuntimeError, match='conflict'):
            queue.remember_native_event(trial['context'], kind, data)
    else:
        queue.remember_native_event(trial['context'], kind, data)
    queue.write_marker(grant['repo_path'], provider, 'fast')
    assert queue.claim_native(grant['hash'], provider=provider) is None
    assert receipt(queue, grant) == before
