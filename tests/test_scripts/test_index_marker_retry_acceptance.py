"""Explicit completed-trial acceptance on scratch SQL; no provider is launched."""

import time
from contextlib import closing
from copy import deepcopy

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
from tests.test_scripts.test_index_marker_retry_consumption import issued
from tests.test_scripts.test_index_marker_retry_grant import candidate, helper


def completed(queue, tmp_path, provider="cbm", *, debt=0, origin="pending", mode='full', settlement='settled'):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", mode)
    old = queue.claim(queue.marker_hash(repo), provider=provider)
    queue.restore(old["context"])
    if origin == 'pending':
        retry, grant, request = issued(queue, tmp_path, provider, debt, mode)
    else:
        retry = helper()
        data = {**candidate(queue, tmp_path, provider), 'origin': 'failed',
                'not_after_wall_ns': time.time_ns() + 3600 * 10**9}
        with queue._decision_gate(native=True) as (db, finalize):
            db.execute("INSERT INTO failed SELECT hash,provider,0,repo_path,provider,'full',requested_at,?,"
                       "'attempt budget exhausted',NULL,?,legacy_unqualified,legacy_shared_attempts "
                       "FROM pending WHERE hash=? AND provider=?", (debt, time.time(), data['hash'], provider))
            grant = retry.issue(db, data, queue._native, queue._legs, finalize=finalize)
        prospective = dict(context=dict(hash=data['hash'], provider=provider, claim_id=grant['nonce']), mode='full')
        request = dict(hash=data['hash'], provider=provider, nonce=grant['nonce'],
                       reservation=_reservation(prospective), policy_sha256=data['policy_sha256'])
    with queue._decision_gate(native=True) as (db, finalize):
        claimed, _ = retry.consume(db, request, queue._native, queue._legs,
                                  finalize=finalize, bind_runtime=lambda context, reserved: object())
    assert queue.register_scope(claimed["context"], _registration(claimed)) == "registered"
    assert queue.begin_native(claimed["context"], _intent(queue, claimed)) == "started"
    queue.remember_native_event(claimed["context"], "terminal", _terminal(claimed))
    queue.remember_native_event(claimed["context"], "cleanup", _cleanup(queue, claimed))
    if origin == 'failed':
        with closing(queue._connect()) as db:
            failed = db.execute('SELECT * FROM failed WHERE hash=? AND provider=?',
                                (grant['hash'], provider)).fetchone()
            assert failed is not None
            assert retry._row_digest(failed, queue._native) == grant['request']['snapshot_sha256']
    if settlement == 'retained-owner':
        assert queue.apply_native(claimed['context']) == 'consumed'
        assert queue.get_inflight(grant['hash']) is not None
    _finish_control(queue, claimed)
    if settlement == 'settled':
        assert queue.apply_native(claimed["context"]) == "consumed"
    else:
        assert settlement in ('unapplied', 'retained-owner')
    if origin == 'failed' and settlement != 'unapplied':
        with closing(queue._connect()) as db:
            assert db.execute('SELECT * FROM failed WHERE hash=? AND provider=?',
                              (grant['hash'], provider)).fetchone() is None
    return retry, grant, claimed, old


def accept(queue, retry, grant, *, reference="fixture://verified-recovery", observe=None, final_hook=None, fresh=None):
    operation = getattr(retry, "accept", None)
    assert callable(operation), "completed supervised recovery needs an explicit acceptance transaction"
    request = {key: grant[key] for key in ("hash", "repo_path", "provider", "nonce")}
    request["evidence_reference"] = reference
    if observe is None:
        def observe():
            return dict(source=grant["source"], policy_sha256=grant["policy_sha256"])
    with queue._decision_gate(native=True) as (db, finalize):
        observed = False
        def observe_final():
            nonlocal observed
            observed = True
            return observe()
        def final(**kwargs):
            if final_hook is not None and observed:
                final_hook(db)
            return finalize(**kwargs)
        return operation(db, request, queue._native, queue._legs, finalize=final,
                         observe_inputs=observe_final, fresh_cleanup=queue._fresh_gate(db) if fresh is None else fresh)


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
def test_exact_completed_selected_trial_accepts_once_without_erasing_hazard(queue, tmp_path, provider):
    retry, grant, claimed, old = completed(queue, tmp_path, provider)
    with closing(queue._connect()) as db:
        assert queue._recovery.provider_blocked(db, grant["hash"], provider, queue._native)
        clock = queue.get_repo_state(grant["hash"])
    result = accept(queue, retry, grant)
    assert result["state"] == "accepted" and result["launch_authority"] is False
    assert result["trial"] == claimed["context"]
    duplicate = accept(queue, retry, grant)
    assert duplicate["state"] == "already_accepted"
    assert duplicate["receipt"] == result["receipt"]
    assert queue.get_repo_state(grant["hash"]) == clock
    with closing(queue._connect()) as db:
        assert not queue._recovery.provider_blocked(db, grant["hash"], provider, queue._native)
        assert db.execute("SELECT safety_kind FROM executions WHERE execution_seq=?",
                          (old["context"]["execution_seq"],)).fetchone()[0] == "uncertainty"
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 2
    queue.write_marker(grant["repo_path"], provider, "fast")
    assert queue.claim_native(grant["hash"], provider=provider) is not None


def receipt(queue, grant):
    with closing(queue._connect()) as db:
        state = dict(db.execute("SELECT * FROM provider_state WHERE hash=? AND provider=?",
                                (grant["hash"], grant["provider"])).fetchone())
    return {key: value for key, value in state.items() if key.startswith("accepted_")}


@pytest.mark.parametrize("fault", ["nonce", "source", "policy", "origin", "debt", "cleanup", "control", "reference"])
def test_invalid_trial_or_inputs_never_publish_acceptance(queue, tmp_path, fault):
    retry, grant, claimed, _ = completed(queue, tmp_path)
    before = receipt(queue, grant)
    selected = deepcopy(grant)
    observed = dict(source=grant["source"], policy_sha256=grant["policy_sha256"])
    reference = "fixture://verified-recovery"
    if fault == "nonce":
        selected["nonce"] = "c" * 32
    elif fault == "source":
        observed["source"] = {**grant["source"], "provider_digest": "f" * 64}
    elif fault == "policy":
        observed["policy_sha256"] = "f" * 64
    elif fault == "reference":
        reference = "bad\0reference"
    else:
        with closing(queue._connect()) as db:
            seq = claimed["context"]["execution_seq"]
            if fault == "origin":
                db.execute("UPDATE executions SET nonce_origin='generated' WHERE execution_seq=?", (seq,))
            elif fault == "debt":
                db.execute("UPDATE executions SET attempts=attempts+1 WHERE execution_seq=?", (seq,))
            elif fault == "cleanup":
                db.execute("UPDATE executions SET cleanup_invalidated=1 WHERE execution_seq=?", (seq,))
            else:
                db.execute("UPDATE executions SET control_closure=NULL WHERE execution_seq=?", (seq,))
    with pytest.raises((ValueError, RuntimeError)):
        accept(queue, retry, selected, observe=lambda: observed, reference=reference)
    assert receipt(queue, grant) == before


@pytest.mark.parametrize("change", ["reference", "trial-disproof"])
def test_accepted_receipt_is_immutable_and_disproof_refuses_duplicate(queue, tmp_path, change):
    retry, grant, claimed, _ = completed(queue, tmp_path)
    accept(queue, retry, grant)
    before = receipt(queue, grant)
    if change == "trial-disproof":
        with pytest.raises(RuntimeError):
            queue.remember_native_event(claimed["context"], "terminal", _terminal(claimed, rc=1))
    with pytest.raises((ValueError, RuntimeError)):
        accept(queue, retry, grant, reference="fixture://changed" if change == "reference" else "fixture://verified-recovery")
    assert receipt(queue, grant) == before


def test_expiry_and_boot_change_cannot_force_a_completed_trial_to_run_again(queue, tmp_path, monkeypatch):
    retry, grant, _, _ = completed(queue, tmp_path)
    expired = dict(boot_id="00000000-0000-0000-0000-000000000001",
                   wall_ns=grant["not_after_wall_ns"] + 1,
                   monotonic_ns=grant["not_after_monotonic_ns"] + 1)
    with pytest.raises(ValueError):
        retry.fresh(grant, expired, queue._native)
    monkeypatch.setitem(retry.accept.__globals__, "_clock", lambda: expired)
    assert accept(queue, retry, grant)["state"] == "accepted"
    with closing(queue._connect()) as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 2


def test_newer_hazard_published_at_final_drain_prevents_acceptance(queue, tmp_path):
    retry, grant, _, _ = completed(queue, tmp_path)
    before = receipt(queue, grant)
    def publish(db):
        db.execute("INSERT INTO executions(hash,repo_path,provider,protocol,entry_kind,claim_id,original_tools,"
                   "mode,requested_at,attempts,legacy_unqualified,legacy_shared_attempts,nonce_origin,safety_kind) "
                   "VALUES(?,?,?,1,'legacy-observation',NULL,'cbm','full',1,0,1,'[]','fixture-late','uncertainty')",
                   (grant["hash"], grant["repo_path"], grant["provider"]))
    with pytest.raises(ValueError, match="newer hazard"):
        accept(queue, retry, grant, final_hook=publish)
    assert receipt(queue, grant) == before


def test_receipt_commit_failure_rolls_back_and_resumes_same_trial(queue, tmp_path, monkeypatch):
    retry, grant, _, _ = completed(queue, tmp_path)
    before = receipt(queue, grant)
    original = queue._connect
    class FailingCommit:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, name): return getattr(self.connection, name)
        def commit(self): raise OSError("fixture commit failure")
    with monkeypatch.context() as patch:
        patch.setitem(queue._decision_gate.__wrapped__.__globals__, "_connect", lambda *a, **kw: FailingCommit(original(*a, **kw)))
        with pytest.raises(OSError, match="commit failure"):
            accept(queue, retry, grant)
    assert receipt(queue, grant) == before
    assert accept(queue, retry, grant)["state"] == "accepted"


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
@pytest.mark.parametrize('origin', ['pending', 'failed'])
@pytest.mark.parametrize('debt', [0, 4, 5, 7])
def test_acceptance_preserves_original_debt_failed_record_and_independent_pending(queue, tmp_path, provider, origin, debt):
    retry, grant, claimed, _ = completed(queue, tmp_path, provider, debt=debt, origin=origin)
    before = [{key: value for key, value in row.items() if key != 'age_s'} for row in queue.list_markers()]
    with closing(queue._connect()) as db:
        failed = [tuple(row) for row in db.execute('SELECT * FROM failed ORDER BY hash,provider')]
    assert accept(queue, retry, grant)['state'] == 'accepted'
    assert [{key: value for key, value in row.items() if key != 'age_s'} for row in queue.list_markers()] == before
    with closing(queue._connect()) as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM failed ORDER BY hash,provider')] == failed
        assert retry.load(db, grant['hash'], provider, queue._native)['status'] == 'spent'
        row = queue._native.execution(db, claimed['context'], queue._legs)
        assert row['attempts'] == debt
        assert row['legacy_shared_attempts'] == grant['request']['legacy_shared_attempts']


@pytest.mark.parametrize('fault', ['partial-receipt', 'open-control', 'unknown', 'resource'])
def test_nonclean_or_incomplete_state_never_releases_provider(queue, tmp_path, fault):
    retry, grant, claimed, _ = completed(queue, tmp_path)
    with closing(queue._connect()) as db:
        seq = claimed['context']['execution_seq']
        if fault == 'partial-receipt':
            db.execute("UPDATE provider_state SET accepted_evidence_reference='partial' WHERE hash=? AND provider=?",
                       (grant['hash'], 'cbm'))
        elif fault == 'open-control':
            db.execute("UPDATE executions SET control_closure=NULL WHERE execution_seq=?", (seq,))
        elif fault == 'unknown':
            db.execute("UPDATE executions SET typed_applied_state=NULL,completion_kind='unknown' WHERE execution_seq=?", (seq,))
        else:
            db.execute("UPDATE executions SET safety_kind='resource' WHERE execution_seq=?", (seq,))
    before = receipt(queue, grant)
    with pytest.raises((ValueError, RuntimeError)):
        accept(queue, retry, grant)
    assert receipt(queue, grant) == before


def test_new_full_request_cannot_be_covered_by_completed_fast_trial(queue, tmp_path):
    retry, grant, _, _ = completed(queue, tmp_path, 'gitnexus', mode='fast')
    queue.write_marker(grant['repo_path'], 'gitnexus', 'full')
    before = receipt(queue, grant)
    with pytest.raises(ValueError, match='workload envelope'):
        accept(queue, retry, grant)
    assert receipt(queue, grant) == before
    assert next(row for row in queue.list_markers() if row['provider'] == 'gitnexus')['mode'] == 'full'


def accounting_snapshot(queue):
    """Only evidence import is allowed; settlement, clocks and work are not."""
    with closing(queue._connect()) as db:
        return {
            table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ('inflight', 'pending', 'failed', 'repo_state', 'provider_state')
        } | {'settlement': [tuple(row) for row in db.execute(
            'SELECT execution_seq,typed_applied_state,typed_charged_attempts,disposition,completion_kind '
            'FROM executions ORDER BY execution_seq')]}


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
@pytest.mark.parametrize('origin', ['pending', 'failed'])
@pytest.mark.parametrize('debt', [0, 4, 5, 7])
@pytest.mark.parametrize('settlement', ['unapplied', 'retained-owner'])
def test_acceptance_cannot_settle_or_retire_an_owner(queue, tmp_path, provider, origin, debt, settlement):
    retry, grant, trial, _ = completed(queue, tmp_path, provider, origin=origin, debt=debt, settlement=settlement)
    row = queue.execution_status(trial['context'])
    assert row['completion_kind'] == 'success' and row['control_closure']['data']['state'] == 'clean'
    assert row['typed_applied_state'] == (None if settlement == 'unapplied' else 'consumed')
    assert queue.get_inflight(grant['hash'])['claim_id'] == trial['context']['claim_id']
    before = accounting_snapshot(queue)
    with pytest.raises((ValueError, RuntimeError), match='settled successful|inflight ownership'):
        accept(queue, retry, grant)
    assert accounting_snapshot(queue) == before
    # The separate original reducer owns accounting and pointer retirement.
    assert queue.apply_native(trial['context']) == 'consumed'
    assert queue.get_inflight(grant['hash']) is None
    settled = accounting_snapshot(queue)
    assert accept(queue, retry, grant)['state'] == 'accepted'
    after = accounting_snapshot(queue)
    for field in ('inflight', 'pending', 'failed', 'repo_state', 'settlement'):
        assert after[field] == settled[field]


@pytest.mark.parametrize('reference,valid', [('x' * 4096, True), ('x' * 4097, False),
                                          ('雪' * 1365 + 'x', True), ('雪' * 1366, False)])
def test_acceptance_reference_uses_existing_utf8_byte_limit(queue, tmp_path, reference, valid):
    retry, grant, _, _ = completed(queue, tmp_path)
    before = receipt(queue, grant)
    if valid:
        assert accept(queue, retry, grant, reference=reference)['state'] == 'accepted'
    else:
        with pytest.raises(ValueError, match='UTF8 byte limit'):
            accept(queue, retry, grant, reference=reference)
        assert receipt(queue, grant) == before


def test_replaced_failed_generation_survives_acceptance(queue, tmp_path):
    retry, grant, _, _ = completed(queue, tmp_path, origin='failed', debt=7)
    # Settlement already retired the exact frozen failed row. This is new work.
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute("INSERT INTO failed SELECT hash,provider,0,repo_path,provider,'fast',requested_at,2,"
                   "'independent later failure',NULL,?,legacy_unqualified,legacy_shared_attempts "
                   "FROM pending WHERE hash=? AND provider=?", (time.time(), grant['hash'], grant['provider']))
        replacement = dict(db.execute('SELECT * FROM failed WHERE hash=? AND provider=?',
                                     (grant['hash'], grant['provider'])).fetchone())
        assert retry._row_digest(replacement, queue._native) != grant['request']['snapshot_sha256']
    assert accept(queue, retry, grant)['state'] == 'accepted'
    with closing(queue._connect()) as db:
        assert dict(db.execute('SELECT * FROM failed WHERE hash=? AND provider=?',
                               (grant['hash'], grant['provider'])).fetchone()) == replacement


def test_conflict_reason_reholds_provider_without_erasing_accepted_receipt(queue, tmp_path):
    retry, grant, trial, _ = completed(queue, tmp_path)
    assert accept(queue, retry, grant)['state'] == 'accepted'
    before = receipt(queue, grant)
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute("UPDATE executions SET conflict_reason='retained contradiction' WHERE execution_seq=?",
                   (trial['context']['execution_seq'],))
        state = db.execute('SELECT * FROM provider_state WHERE hash=? AND provider=?',
                           (grant['hash'], grant['provider'])).fetchone()
        assert queue._recovery._accepted_clean(db, state, queue._native) is False
        assert queue._recovery.provider_blocked(db, grant['hash'], grant['provider'], queue._native)
    queue.write_marker(grant['repo_path'], grant['provider'], 'fast')
    assert queue.claim_native(grant['hash'], provider=grant['provider']) is None
    assert receipt(queue, grant) == before
