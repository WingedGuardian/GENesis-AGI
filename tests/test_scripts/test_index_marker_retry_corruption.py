"""Broken metadata is observable/revocable, while launch remains strict."""

import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from copy import deepcopy

import pytest

from tests.test_scripts import test_index_marker_control as control
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _reservation
from tests.test_scripts.test_index_marker_retry_grant import NOW, candidate, helper

KINDS = ("json", "utf8-blob", "utf8-text", "integer", "boot", "shared", "source", "deadline", "deep")


def typed_raw(queue, h, provider):
    with closing(queue._connect()) as db:
        row = db.execute("SELECT typeof(grant),CAST(grant AS BLOB) FROM provider_state "
                         "WHERE hash=? AND provider=?", (h, provider)).fetchone()
    storage, raw = row
    digest = hashlib.sha256(storage.encode("ascii") + b"\0" + raw).hexdigest() if raw is not None else None
    return storage, raw, digest


def corrupt(queue, tmp_path, provider, kind, *, history=False):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    data["nonce"] = "c" * 32
    now = dict(boot_id=queue._recovery.owner_identity()["boot_id"],
               wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())
    data["not_after_wall_ns"] = now["wall_ns"] + 3600 * 10**9
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native, queue._legs, now=now, finalize=finalize)
    original = None
    if history:
        claimed = queue.claim_native(data["hash"], provider=provider)
        reserved = _reservation(claimed)
        assert queue.reserve_launch(claimed["context"], reserved) == "reserved"
        queue.cancel_launch(claimed["context"])
        closure = control._closure(queue, reserved, state="uncertain", reason="startup_incomplete")
        for key, pid, rc in (("foreground", 1234, 125), ("monitor", 1235, 0)):
            closure[key] = dict(identity={**reserved["owner"], "pid": pid}, returncode=rc)
        control._publish(queue, claimed, closure)
        queue.reconcile_native()
        status = queue.execution_status(claimed["context"])
        assert status["safety_kind"] == "uncertainty"
        original = dict(context=claimed["context"], status=status)
    value = deepcopy(grant)
    if kind == "boot":
        value["issued_boot_id"] = 7
    elif kind == "shared":
        value["request"]["legacy_shared_attempts"] = []
    elif kind == "source":
        value["source"] = {}
    elif kind == "deadline":
        value["not_after_wall_ns"] = 0
    raw = (b'{"fixture":' if kind == "json" else b"\xff" if kind.startswith("utf8")
           else 7 if kind == "integer" else b"[" * 1100 + b"0" + b"]" * 1100 if kind == "deep"
           else queue._native.encode(value))
    with closing(queue._connect()) as db, queue._transaction(db):
        statement = ("UPDATE provider_state SET grant=CAST(? AS TEXT) WHERE hash=? AND provider=?"
                     if kind == "utf8-text" else
                     "UPDATE provider_state SET grant=? WHERE hash=? AND provider=?")
        db.execute(statement, (raw, data["hash"], provider))
    return retry, data, original


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("kind", KINDS)
def test_unrelated_corrupt_grant_never_prevents_fresh_issue(queue, tmp_path, provider, kind):
    other = "gitnexus" if provider == "cbm" else "cbm"
    retry, data, _ = corrupt(queue, tmp_path / "other", other, kind)
    before = typed_raw(queue, data["hash"], other)
    target = candidate(queue, tmp_path / "target", provider)
    target["not_after_wall_ns"] = time.time_ns() + 3600 * 10**9
    with queue._decision_gate(native=True) as (db, finalize):
        issued = retry.issue(db, target, queue._native, queue._legs, finalize=finalize)
        assert issued["status"] == "issued" and issued["provider"] == provider
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 0
    assert typed_raw(queue, data["hash"], other) == before


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("kind", KINDS)
def test_status_observes_invalid_metadata_and_independent_hold(
    queue, tmp_path, capsys, provider, kind
):
    retry, data, original = corrupt(queue, tmp_path, provider, kind, history=True)
    before = typed_raw(queue, data["hash"], provider)
    args = argparse.Namespace(cmd="status-recovery", repo=data["repo_path"], provider=provider)
    assert retry.dispatch(args, vars(queue), None) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["launch_authority"] is False
    assert result["recorded_provider_hold"] is True
    assert result["grant"] == dict(state="invalid-grant", storage_type=before[0],
                                   size_bytes=len(before[1]), sha256=before[2], launch_authority=False)
    assert typed_raw(queue, data["hash"], provider) == before
    assert queue.execution_status(original["context"]) == original["status"]


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("matching", [False, True])
def test_nonce_scan_preserves_real_collisions_even_with_invalid_other_fields(
    queue, tmp_path, provider, reverse, matching
):
    other = "gitnexus" if provider == "cbm" else "cbm"
    retry, data, _ = corrupt(queue, tmp_path / "other", other, "source")
    target = candidate(queue, tmp_path / "target", provider)
    target["not_after_wall_ns"] = time.time_ns() + 3600 * 10**9
    with queue._decision_gate(native=True) as (db, finalize):
        value = json.loads(db.execute("SELECT grant FROM provider_state WHERE hash=? AND provider=?",
                                      (data["hash"], other)).fetchone()[0])
        assert value["source"] == {}
        if matching:
            value["nonce"] = target["nonce"]
        db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider=?",
                   (queue._native.encode(value), data["hash"], other))
        db.execute("PRAGMA reverse_unordered_selects=" + str(int(reverse)))
        if matching:
            with pytest.raises(ValueError, match="nonce is already current"):
                retry.issue(db, target, queue._native, queue._legs, finalize=finalize)
            assert retry.load(db, target["hash"], provider, queue._native) is None
        else:
            assert retry.issue(db, target, queue._native, queue._legs, finalize=finalize)["status"] == "issued"


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("status", ["issued", "revoked", "spent"])
def test_digest_selector_never_removes_healthy_metadata(queue, tmp_path, provider, status):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
        grant["status"] = status
        if status == "spent":
            grant["trial"] = dict(hash=data["hash"], repo_path=data["repo_path"], provider=provider,
                                  protocol=3, execution_seq=1, claim_id=data["nonce"])
        retry.validate(grant, queue._native)
        db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider=?",
                   (queue._native.encode(grant), data["hash"], provider))
    before = typed_raw(queue, data["hash"], provider)
    with pytest.raises(ValueError, match="valid grant"), queue._decision_gate(native=True) as (db, finalize):
        retry.revoke(db, data["hash"], provider, None, queue._native, grant_sha256=before[2])
    assert typed_raw(queue, data["hash"], provider) == before
    if status == "spent":
        with queue._decision_gate(native=True) as (db, finalize):
            assert retry.revoke(db, data["hash"], provider, data["nonce"], queue._native) == grant
        assert typed_raw(queue, data["hash"], provider) == before


@pytest.mark.parametrize("operation", ["load", "observe_grant", "_nonce_used"])
def test_database_failure_is_not_classified_as_invalid_content(queue, operation):
    retry = helper()
    with (closing(sqlite3.connect(":memory:")) as db,
          pytest.raises(sqlite3.OperationalError, match="no such table")):
        if operation == "_nonce_used":
            retry._nonce_used(db, "b" * 32)
        else:
            getattr(retry, operation)(db, "a" * 16, "cbm", queue._native)


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("operation", ["issue", "revoke"])
def test_commit_failure_preserves_current_grant_and_work(queue, tmp_path, provider, operation):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    h = data["hash"]
    with queue._decision_gate(native=True) as (db, finalize):
        retry.issue(db, {**data, "nonce": "c" * 32}, queue._native, queue._legs,
                    now=NOW, finalize=finalize)
        if operation == "revoke":
            db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider=?", (b"{broken", h, provider))
    before = typed_raw(queue, h, provider)
    with closing(queue._connect()) as db:
        pending = [tuple(r) for r in db.execute("SELECT * FROM pending ORDER BY hash,provider")]
    staged = []
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM provider_state").fetchall()
        with pytest.raises(sqlite3.OperationalError, match="locked"), queue._decision_gate(native=True) as (db, finalize):
            db.execute("PRAGMA busy_timeout=1")
            if operation == "issue":
                result = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
                assert result["nonce"] == data["nonce"]
            else:
                result = retry.revoke(db, h, provider, None, queue._native, grant_sha256=before[2])
                assert result["state"] == "revoked-corrupt"
            staged.append(True)  # The mutation succeeded; only COMMIT is refused.
        reader.rollback()
    assert staged == [True]
    assert typed_raw(queue, h, provider) == before
    with closing(queue._connect()) as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM pending ORDER BY hash,provider")] == pending
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 0


def test_corrupt_metadata_status_and_revoke_preserve_a_valid_retained_acceptance(queue, tmp_path, capsys):
    from tests.test_scripts.test_index_marker_provider_hold import _accepted

    retry = helper()
    _, claimed, _, _, _ = _accepted(queue, tmp_path)
    h = claimed["hash"]
    with closing(queue._connect()) as db, queue._transaction(db):
        before = dict(queue._recovery.provider_snapshot(db, h, "cbm"))
        assert queue._recovery._accepted_clean(db, before, queue._native)
        assert before["accepted_sequence"] > 0 and before["hazard_revision"] == 17
        db.execute("UPDATE provider_state SET grant=CAST(? AS TEXT) WHERE hash=? AND provider='cbm'", (b"\xff", h))
    args = argparse.Namespace(cmd="status-recovery", repo=claimed["repo_path"], provider="cbm")
    assert retry.dispatch(args, vars(queue), None) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["grant"]["state"] == "invalid-grant"
    assert observed["recorded_provider_hold"] is False
    with queue._decision_gate(native=True) as (db, finalize):
        assert dict(queue._recovery.provider_snapshot(db, h, "cbm")) == before
        assert retry.revoke(db, h, "cbm", None, queue._native,
                            grant_sha256=observed["grant"]["sha256"])["state"] == "revoked-corrupt"
        assert dict(queue._recovery.provider_snapshot(db, h, "cbm")) == before
        assert queue._recovery._accepted_clean(db, before, queue._native)
        assert not queue._recovery.provider_blocked(db, h, "cbm", queue._native)


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("kind", KINDS)
def test_exact_corrupt_revocation_preserves_work_and_recorded_uncertainty(
    queue, tmp_path, provider, kind
):
    retry, data, original = corrupt(queue, tmp_path, provider, kind, history=True)
    h = data["hash"]
    before = typed_raw(queue, h, provider)
    with closing(queue._connect()) as db:
        pending = [tuple(row) for row in db.execute("SELECT * FROM pending ORDER BY hash,provider")]
    with pytest.raises(ValueError), queue._decision_gate(native=True) as (db, finalize):
        retry.revoke(db, h, provider, None, queue._native, grant_sha256="d" * 64)
    assert typed_raw(queue, h, provider) == before
    with queue._decision_gate(native=True) as (db, finalize):
        result = retry.revoke(db, h, provider, None, queue._native, grant_sha256=before[2])
        assert result == dict(state="revoked-corrupt", sha256=before[2], launch_authority=False)
        assert queue._recovery.provider_blocked(db, h, provider, queue._native)
        assert [tuple(row) for row in db.execute("SELECT * FROM pending ORDER BY hash,provider")] == pending
    assert typed_raw(queue, h, provider)[:2] == ("null", None)
    assert queue.execution_status(original["context"]) == original["status"]
