"""Prior selected executions settle conservatively without new launch authority."""

import json
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _finish_control
from tests.test_scripts.test_index_marker_native_replay import _cleanup, _terminal
from tests.test_scripts.test_index_marker_retry_grant import helper
from tests.test_scripts.test_index_marker_retry_outcomes import selected_failure, stored


def provider_state(queue, claimed):
    with closing(queue._connect()) as db:
        return queue._recovery.provider_snapshot(db, claimed["hash"], claimed["provider"])


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("outcome", ["success", "failure", "resource", "unstarted"])
@pytest.mark.parametrize("damage", ["missing", "malformed", "unspent", "trial", "request", "source"])
def test_unavailable_selected_binding_preserves_both_tables_and_charges_once(
    queue, tmp_path, provider, outcome, damage
):
    claimed, failed, pending = selected_failure(queue, tmp_path, provider, 4, True, outcome)
    context, h = claimed["context"], claimed["hash"]
    with closing(queue._connect()) as db, queue._transaction(db):
        grant = json.loads(db.execute("SELECT grant FROM provider_state WHERE hash=? AND provider=?",
                                    (h, provider)).fetchone()[0])
        if damage == "unspent":
            grant.update(status="issued", trial=None)
        elif damage == "trial":
            grant["trial"]["execution_seq"] += 1
        elif damage == "request":
            grant["request"]["attempts"] += 1
        elif damage == "source":
            grant["source"]["provider_digest"] = "e" * 64
        raw = None if damage == "missing" else b"{broken" if damage == "malformed" else queue._native.encode(grant)
        db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider=?", (raw, h, provider))
    if outcome != "unstarted":
        queue.remember_native_event(context, "terminal", _terminal(
            claimed, rc=int(outcome == "failure"), oom=int(outcome == "resource")))
        queue.remember_native_event(context, "cleanup", _cleanup(
            queue, claimed, oom=int(outcome == "resource")))
    _finish_control(queue, claimed)
    before = provider_state(queue, claimed)
    queue.reconcile_native()
    assert queue.apply_native(context) == "failed"
    status, state = queue.execution_status(context), provider_state(queue, claimed)
    assert status["disposition"] == "selected-grant-unavailable"
    assert status["safety_kind"] == ("resource" if outcome == "resource" else "uncertainty")
    assert status["typed_charged_attempts"] == 4 + int(outcome in ("failure", "resource"))
    assert state["hazard_revision"] == before["hazard_revision"] + int(outcome != "resource")
    assert stored(queue, "failed", h) == failed
    assert stored(queue, "pending", h) == pending
    assert queue.get_inflight(h) is None  # Independent physical/control proof is complete.
    with closing(queue._connect()) as db:
        assert queue._recovery.provider_blocked(db, h, provider, queue._native)
    queue.reconcile_native()
    assert queue.apply_native(context) == "failed"
    assert queue.execution_status(context) == status
    assert provider_state(queue, claimed) == state
    assert stored(queue, "failed", h) == failed
    assert stored(queue, "pending", h) == pending


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("replacement", ["changed", "absent"])
def test_success_cannot_retire_a_different_failure_or_independent_pending(
    queue, tmp_path, provider, replacement
):
    claimed, _, pending = selected_failure(queue, tmp_path, provider, 2, True, "success")
    context, h = claimed["context"], claimed["hash"]
    with closing(queue._connect()) as db, queue._transaction(db):
        if replacement == "changed":
            db.execute("UPDATE failed SET reason='new independent incident',raw_payload=? WHERE hash=? AND provider=?",
                       (b"new evidence", h, provider))
        else:
            db.execute("DELETE FROM failed WHERE hash=? AND provider=?", (h, provider))
    failures = stored(queue, "failed", h)
    queue.remember_native_event(context, "terminal", _terminal(claimed))
    queue.remember_native_event(context, "cleanup", _cleanup(queue, claimed))
    _finish_control(queue, claimed)
    assert queue.apply_native(context) == "consumed"
    assert stored(queue, "failed", h) == failures
    assert stored(queue, "pending", h) == pending
    assert queue.execution_status(context)["typed_charged_attempts"] == 2


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
def test_unknown_selected_completion_keeps_original_failure_and_control_hold(queue, tmp_path, provider):
    claimed, failed, pending = selected_failure(queue, tmp_path, provider, 2, True, "unknown")
    context, h = claimed["context"], claimed["hash"]
    queue.remember_native_event(context, "cleanup", _cleanup(queue, claimed))
    assert queue.apply_native(context) == "failed"
    status = queue.execution_status(context)
    assert (status["completion_kind"], status["safety_kind"], status["typed_charged_attempts"]) == (
        "unknown", "uncertainty", 3)
    assert queue.get_inflight(h)["claim_id"] == context["claim_id"]
    assert stored(queue, "failed", h) == failed
    assert stored(queue, "pending", h) == pending
    _finish_control(queue, claimed)
    assert queue.apply_native(context) == "failed"
    assert queue.get_inflight(h) is None
    assert queue.execution_status(context)["typed_charged_attempts"] == 3


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("outcome", ["success", "failure", "resource", "unstarted"])
def test_valid_pending_origin_retains_ordinary_restore_backoff_and_replay(queue, tmp_path, provider, outcome):
    claimed, failed, pending = selected_failure(queue, tmp_path, provider, 2, True, outcome, origin="pending")
    context, h = claimed["context"], claimed["hash"]
    assert claimed["attempts"] == 1 and claimed["mode"] == "fast"
    if outcome != "unstarted":
        queue.remember_native_event(context, "terminal", _terminal(
            claimed, rc=int(outcome == "failure"), oom=int(outcome == "resource")))
        queue.remember_native_event(context, "cleanup", _cleanup(queue, claimed, oom=int(outcome == "resource")))
    _finish_control(queue, claimed)
    queue.reconcile_native()
    expected = "consumed" if outcome == "success" else "pending"
    assert queue.apply_native(context) == expected
    status = queue.execution_status(context)
    backoff = provider == "cbm" and outcome == "failure"
    assert status["typed_charged_attempts"] == 1 + int(outcome in ("failure", "resource") and not backoff)
    assert stored(queue, "failed", h) == failed
    rows = stored(queue, "pending", h)
    assert [row for row in rows if row["provider"] != provider] == pending
    target = [row for row in rows if row["provider"] == provider]
    if outcome == "success":
        assert not target
    else:
        assert len(target) == 1 and target[0]["mode"] == "fast"
        assert target[0]["attempts"] == status["typed_charged_attempts"]
    with closing(queue._connect()) as db:
        meta = db.execute("SELECT last_full,full_backoff FROM repo_state WHERE hash=?", (h,)).fetchone()
        assert (meta[0] is not None) == (provider == "cbm" and outcome == "success")
        assert (meta[1] is not None) == backoff
    queue.reconcile_native()
    assert queue.apply_native(context) == expected
    assert queue.execution_status(context) == status
    assert stored(queue, "pending", h) == rows


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("outcome", ["success", "failure", "unstarted"])
@pytest.mark.parametrize("replacement", [None, b"{broken"])
def test_applied_trial_replay_ignores_subsequently_removed_or_corrupt_grant(
    queue, tmp_path, provider, outcome, replacement
):
    claimed, _, _ = selected_failure(queue, tmp_path, provider, 2, True, outcome)
    context, h = claimed["context"], claimed["hash"]
    if outcome != "unstarted":
        queue.remember_native_event(context, "terminal", _terminal(claimed, rc=int(outcome == "failure")))
        queue.remember_native_event(context, "cleanup", _cleanup(queue, claimed))
    _finish_control(queue, claimed)
    queue.reconcile_native()
    expected = "consumed" if outcome == "success" else "failed"
    assert queue.apply_native(context) == expected
    status, safety = queue.execution_status(context), provider_state(queue, claimed)
    work = {table: stored(queue, table, h) for table in ("pending", "failed")}
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider=?", (replacement, h, provider))
    queue.reconcile_native()
    assert queue.apply_native(context) == expected
    assert queue.execution_status(context) == status
    assert provider_state(queue, claimed) == safety
    assert {table: stored(queue, table, h) for table in work} == work


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
def test_expired_spent_grant_still_owes_truthful_settlement(queue, tmp_path, provider):
    retry = helper()
    claimed, failed, pending = selected_failure(queue, tmp_path, provider, 2, True, "success")
    context, h = claimed["context"], claimed["hash"]
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.load(db, h, provider, queue._native)
        grant.update(issued_wall_ns=1, issued_monotonic_ns=1,
                     not_after_wall_ns=2, not_after_monotonic_ns=2)
        retry.validate(grant, queue._native)
        with pytest.raises(ValueError, match="expired"):
            retry.fresh(grant, retry._clock(), queue._native)
        db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider=?",
                   (queue._native.encode(grant), h, provider))
    queue.remember_native_event(context, "terminal", _terminal(claimed))
    queue.remember_native_event(context, "cleanup", _cleanup(queue, claimed))
    _finish_control(queue, claimed)
    assert queue.apply_native(context) == "consumed"
    assert stored(queue, "failed", h) == [row for row in failed if row["provider"] != provider]
    assert stored(queue, "pending", h) == pending
    assert queue.execution_status(context)["typed_charged_attempts"] == 2
