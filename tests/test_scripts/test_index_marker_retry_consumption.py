"""One admitted transaction binds a selected trial to its actual owner."""

import time
from contextlib import closing

import pytest

from tests.test_scripts import test_index_marker_control as control
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _reservation
from tests.test_scripts.test_index_marker_retry_grant import candidate, helper


def issued(queue, tmp_path, provider="cbm", debt=0, mode="full"):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    data["run_mode"] = mode
    now = dict(boot_id=queue._recovery.owner_identity()["boot_id"],
               wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())
    data["not_after_wall_ns"] = now["wall_ns"] + 3600 * 10**9
    with queue._decision_gate(native=True) as (db, finalize):
        db.execute("UPDATE pending SET attempts=?,mode=? WHERE hash=? AND provider=?",
                   (debt, mode, data["hash"], provider))
        grant = retry.issue(db, data, queue._native, queue._legs, now=now, finalize=finalize)
    provisional = dict(context=dict(hash=data["hash"], provider=provider, claim_id=grant["nonce"]),
                       mode=grant["run_mode"])
    request = dict(hash=data["hash"], provider=provider, nonce=grant["nonce"],
                   reservation=_reservation(provisional), policy_sha256=data["policy_sha256"])
    return retry, grant, request


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("debt", [0, 5, 7])
def test_selected_consumption_never_commits_an_open_unreserved_trial(
    queue, tmp_path, provider, debt
):
    retry, grant, request = issued(queue, tmp_path, provider, debt)
    consume = getattr(retry, "consume", None)
    assert callable(consume), "selected consumption must reserve its actual owner atomically"
    runtime = object()
    calls = []
    with queue._decision_gate(native=True) as (db, finalize):
        def bind(context, reserved):
            row = queue._native.execution(db, context, queue._legs)
            assert row["launch_permission"] == "reserved"
            assert row["launch_reservation"] is not None
            assert reserved["owner"] == queue._recovery.owner_identity()
            assert retry.load(db, grant["hash"], provider, queue._native)["status"] == "spent"
            calls.append(context)
            return runtime  # Stand-in for the supervisor's closed runtime validator.

        claimed, bound = consume(db, request, queue._native, queue._legs,
                                 finalize=finalize, bind_runtime=bind)
        assert bound is runtime and calls == [claimed["context"]]
    row = queue.execution_status(claimed["context"])
    assert row["nonce_origin"] == "selected-recovery" and row["attempts"] == debt
    assert row["launch_permission"] == "reserved" and row["native_intent"] is None
    assert row["launch_reservation"] == request["reservation"]
    assert row["claim_id"] == grant["nonce"]
    assert [item["provider"] for item in queue.list_markers()] == [
        "gitnexus" if provider == "cbm" else "cbm"
    ]
    with closing(queue._connect()) as db:
        retained = retry.load(db, grant["hash"], provider, queue._native)
        assert retained["trial"] == claimed["context"] and retained["status"] == "spent"
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 1


@pytest.mark.parametrize("fault", ["runtime", "owner", "source"])
def test_failed_consumption_preserves_grant_work_and_no_partial_trial(queue, tmp_path, fault):
    retry, grant, request = issued(queue, tmp_path)
    consume = getattr(retry, "consume", None)
    assert callable(consume), "selected validation failures must leave no partial trial"
    if fault == "owner":
        request["reservation"]["owner"]["pid"] += 1
    elif fault == "source":
        request["reservation"]["source"]["provider_digest"] = "f" * 64

    def bind(context, reserved):
        if fault == "runtime":
            raise ValueError("synthetic invalid runtime")
        return object()

    with pytest.raises(ValueError), queue._decision_gate(native=True) as (db, finalize):
        consume(db, request, queue._native, queue._legs, finalize=finalize, bind_runtime=bind)
    with closing(queue._connect()) as db:
        assert retry.load(db, grant["hash"], "cbm", queue._native) == grant
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 2


def test_duplicate_consumption_has_no_runtime_or_second_trial(queue, tmp_path):
    retry, grant, request = issued(queue, tmp_path)
    consume = getattr(retry, "consume", None)
    assert callable(consume), "a duplicate nonce cannot authorize another owner"
    calls = []

    def bind(context, reserved):
        calls.append(context)
        return object()

    with queue._decision_gate(native=True) as (db, finalize):
        first = consume(db, request, queue._native, queue._legs, finalize=finalize, bind_runtime=bind)
    with queue._decision_gate(native=True) as (db, finalize):
        assert consume(db, request, queue._native, queue._legs,
                       finalize=finalize, bind_runtime=bind) is None
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 1
        assert retry.load(db, grant["hash"], "cbm", queue._native)["trial"] == first[0]["context"]
    assert calls == [first[0]["context"]]


def test_selected_trial_can_reserve_under_covered_hold_without_releasing_it(queue, tmp_path):
    old, reserved = control._reserved(queue, tmp_path, mode="full")
    queue.cancel_launch(old["context"])
    closure = control._closure(queue, reserved, state="uncertain", reason="startup_incomplete")
    closure["foreground"] = {"identity": {**reserved["owner"], "pid": 1234}, "returncode": 125}
    closure["monitor"] = {"identity": {**reserved["owner"], "pid": 1235}, "returncode": 0}
    control._publish(queue, old, closure)
    queue.reconcile_native()
    assert queue.get_inflight(old["hash"]) is None
    assert queue.claim_native(old["hash"], provider="cbm") is None
    retry = helper()
    now = dict(boot_id=queue._recovery.owner_identity()["boot_id"],
               wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())
    data = dict(hash=old["hash"], repo_path=old["repo_path"], provider="cbm", origin="pending",
                run_mode="full", nonce="b" * 32, source=reserved["source"], policy_sha256="a" * 64,
                evidence_reference="fixture://covered-uncertainty",
                not_after_wall_ns=now["wall_ns"] + 3600 * 10**9)
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native, queue._legs, now=now, finalize=finalize)
    prospective = dict(context=dict(hash=old["hash"], provider="cbm", claim_id=grant["nonce"]),
                       mode="full")
    request = dict(hash=old["hash"], provider="cbm", nonce=grant["nonce"],
                   reservation=_reservation(prospective), policy_sha256=data["policy_sha256"])
    with queue._decision_gate(native=True) as (db, finalize):
        claimed, _ = retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
                                   bind_runtime=lambda context, reservation: object())
        assert queue._recovery.provider_blocked(db, old["hash"], "cbm", queue._native)
    assert queue.execution_status(claimed["context"])["launch_permission"] == "reserved"
    assert queue.execution_status(old["context"])["safety_kind"] == "uncertainty"


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("debt", [5, 7, 2**63 - 2, 2**63 - 1])
def test_failed_origin_preserves_real_debt_and_independent_pending(queue, tmp_path, provider, debt):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    data["origin"] = "failed"
    now = dict(boot_id=queue._recovery.owner_identity()["boot_id"],
               wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())
    data["not_after_wall_ns"] = now["wall_ns"] + 3600 * 10**9
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute(
            "INSERT INTO failed SELECT hash,provider,0,repo_path,provider,'full',requested_at,?,"
            "'attempt budget exhausted',NULL,?,legacy_unqualified,legacy_shared_attempts "
            "FROM pending WHERE hash=? AND provider=?",
            (debt, time.time(), data["hash"], provider),
        )
        db.execute("UPDATE pending SET mode='fast' WHERE hash=? AND provider=?", (data["hash"], provider))
        failed = dict(db.execute("SELECT * FROM failed WHERE hash=? AND provider=?",
                                 (data["hash"], provider)).fetchone())
    pending = [{k:v for k,v in row.items() if k != "age_s"} for row in queue.list_markers()]
    if debt == queue._native.INTEGER_MAX:
        with pytest.raises(ValueError, match="exhausted"), queue._decision_gate(native=True) as (db, finalize):
            retry.issue(db, data, queue._native, queue._legs, now=now, finalize=finalize)
        with closing(queue._connect()) as db:
            assert retry.load(db, data["hash"], provider, queue._native) is None
            assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
    else:
        with queue._decision_gate(native=True) as (db, finalize):
            grant = retry.issue(db, data, queue._native, queue._legs, now=now, finalize=finalize)
        prospective = dict(context=dict(hash=data["hash"], provider=provider, claim_id=grant["nonce"]), mode="full")
        request = dict(hash=data["hash"], provider=provider, nonce=grant["nonce"],
                       reservation=_reservation(prospective), policy_sha256=data["policy_sha256"])
        with queue._decision_gate(native=True) as (db, finalize):
            claimed, _ = retry.consume(db, request, queue._native, queue._legs, finalize=finalize,
                                       bind_runtime=lambda context, reserved: object())
        row = queue.execution_status(claimed["context"])
        assert row["attempts"] == debt and row["mode"] == "full"
        assert row["launch_permission"] == "reserved"
    assert [{k:v for k,v in row.items() if k != "age_s"} for row in queue.list_markers()] == pending
    with closing(queue._connect()) as db:
        assert dict(db.execute("SELECT * FROM failed WHERE hash=? AND provider=?",
                               (data["hash"], provider)).fetchone()) == failed
