"""A supervised failed request never becomes automatic pending work on settlement."""

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
from tests.test_scripts.test_index_marker_retry_grant import candidate, helper


def stored(queue, table, h):
    query = {
        "failed": "SELECT * FROM failed WHERE hash=? ORDER BY provider",
        "pending": "SELECT * FROM pending WHERE hash=? ORDER BY provider",
    }[table]
    with closing(queue._connect()) as db:
        return [dict(row) for row in db.execute(query, (h,))]


def selected_failure(queue, tmp_path, provider, debt, pending, outcome, *, origin="failed"):
    """Restore an older selected row; this prefix cannot authorize a new one.

    Existing staging/ordinary proof APIs construct fixture facts, then one
    transaction restores their retained selected provenance and spent grant.
    No manager or provider process runs in this compatibility fixture.
    """
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    data["origin"] = origin
    now = dict(boot_id=queue._recovery.owner_identity()["boot_id"],
               wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())
    data["not_after_wall_ns"] = now["wall_ns"] + 3600 * 10**9
    h = data["hash"]
    sibling = "gitnexus" if provider == "cbm" else "cbm"
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute(
            "INSERT INTO failed SELECT hash,provider,0,repo_path,provider,mode,requested_at,?,"
            "'original resource incident',?,1234,legacy_unqualified,legacy_shared_attempts "
            "FROM pending WHERE hash=? AND provider=?",
            (debt, b"retained original failure", h, provider),
        )
        db.execute(
            "INSERT INTO failed SELECT hash,provider,0,repo_path,provider,mode,requested_at,7,"
            "'independent sibling failure',?,1200,legacy_unqualified,legacy_shared_attempts "
            "FROM pending WHERE hash=? AND provider=?",
            (b"retained sibling failure", h, sibling),
        )
        if pending:
            db.execute("UPDATE pending SET mode='fast',attempts=1 WHERE hash=? AND provider=?",
                       (h, provider))
        else:
            db.execute("DELETE FROM pending WHERE hash=? AND provider=?", (h, provider))
    failed, independent = stored(queue, "failed", h), stored(queue, "pending", h)
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native, queue._legs, now=now, finalize=finalize)
        queries = {"failed": "SELECT * FROM failed WHERE hash=? AND provider=?",
                   "pending": "SELECT * FROM pending WHERE hash=? AND provider=?"}
        source = dict(db.execute(queries[origin], (h, provider)).fetchone())
        claimed = queue._legs._insert_claim(db, h, provider, source, protocol=3,
                                            nonce=grant["nonce"], origin="generated")
        if origin == "pending":
            db.execute("DELETE FROM pending WHERE hash=? AND provider=?", (h, provider))
    assert queue.reserve_launch(claimed["context"], {**_reservation(claimed), "run_mode": grant["run_mode"]}) == "reserved"
    if outcome != "unstarted":
        assert queue.register_scope(claimed["context"], _registration(claimed)) == "registered"
        assert queue.begin_native(claimed["context"], _intent(queue, claimed)) == "started"
    spent = {**grant, "status": "spent", "trial": claimed["context"]}
    retry.validate(spent, queue._native)
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute("UPDATE executions SET nonce_origin='selected-recovery' WHERE execution_seq=?",
                   (claimed["context"]["execution_seq"],))
        db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider=?",
                   (queue._native.encode(spent), h, provider))
    return claimed, failed, stored(queue, "pending", h) if origin == "pending" else independent


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("debt", [2, 5])
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "resource", "unstarted"])
def test_failed_origin_settles_once_without_restoring_automatic_work(
    queue, tmp_path, provider, debt, pending, outcome
):
    claimed, failed, independent = selected_failure(queue, tmp_path, provider, debt, pending, outcome)
    context, h = claimed["context"], claimed["hash"]
    if outcome != "unstarted":
        queue.remember_native_event(context, "terminal", _terminal(
            claimed, rc=int(outcome == "failure"), oom=int(outcome == "resource")))
        queue.remember_native_event(context, "cleanup", _cleanup(
            queue, claimed, oom=int(outcome == "resource")))
    _finish_control(queue, claimed)
    queue.reconcile_native()  # Includes the separate structural-unstarted restore path.
    result = queue.apply_native(context)
    assert result == ("consumed" if outcome == "success" else "failed")
    expected = [row for row in failed if row["provider"] != provider] if outcome == "success" else failed
    assert stored(queue, "failed", h) == expected
    assert stored(queue, "pending", h) == independent
    status = queue.execution_status(context)
    assert status["typed_charged_attempts"] == debt + int(outcome in ("failure", "resource"))
    if outcome == "resource":
        assert status["safety_kind"] == "resource"
        with closing(queue._connect()) as db:
            assert queue._recovery.provider_blocked(db, h, provider, queue._native)
        assert queue.claim_native(h, provider=provider) is None
    queue.reconcile_native()
    assert queue.apply_native(context) == result
    assert queue.execution_status(context) == status
    assert stored(queue, "failed", h) == expected
    assert stored(queue, "pending", h) == independent
