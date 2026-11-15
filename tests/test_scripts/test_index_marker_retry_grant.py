"""Explicit grant storage never claims work or bypasses old ownership."""

import json
import runpy
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _source

BOOT = "00000000-0000-0000-0000-000000000001"
NOW = dict(boot_id=BOOT, wall_ns=1000, monotonic_ns=2000)


def helper():
    path = Path(__file__).resolve().parents[2] / "scripts/lib/index_marker_retry.py"
    assert path.is_file(), "explicit recovery needs the existing provider grant lifecycle"
    return SimpleNamespace(**runpy.run_path(str(path)))


def candidate(queue, tmp_path, provider="cbm"):
    repo = str(tmp_path / "repo")
    queue.write_marker(repo, "both", "full")
    return dict(
        hash=queue.marker_hash(repo),
        repo_path=repo,
        provider=provider,
        origin="pending",
        run_mode="full",
        nonce="b" * 32,
        source=_source(),
        policy_sha256="a" * 64,
        evidence_reference="fixture://supervised-run",
        not_after_wall_ns=11000,
    )


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
def test_issue_is_one_current_metadata_grant_without_work_or_attempt_mutation(
    queue, tmp_path, provider
):
    retry = helper()
    data = candidate(queue, tmp_path, provider)
    before = [{k: v for k, v in row.items() if k != "age_s"} for row in queue.list_markers()]
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
        assert grant["status"] == "issued" and grant["trial"] is None
        assert grant["nonce"] == data["nonce"] and grant["request"]["attempts"] == 0
        assert grant["not_after_monotonic_ns"] == 12000
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 0
    with closing(queue._connect()) as db:
        assert retry.load(db, data["hash"], provider, queue._native) == grant
        assert db.execute("SELECT count(*) FROM provider_state").fetchone()[0] == 1
    assert [
        {k: v for k, v in row.items() if k != "age_s"} for row in queue.list_markers()
    ] == before


@pytest.mark.parametrize("boundary", [1000, 999, True, -1, 2**63])
def test_explicit_deadline_is_required_future_signed64_not_a_default(queue, tmp_path, boundary):
    retry = helper()
    data = {**candidate(queue, tmp_path), "not_after_wall_ns": boundary}
    with queue._decision_gate(native=True) as (db, finalize):
        with pytest.raises(ValueError):
            retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
        assert db.execute("SELECT count(*) FROM provider_state").fetchone()[0] == 0


def test_monotonic_derivation_cannot_overflow_or_renew_by_wall_clock_rollback(queue, tmp_path):
    retry = helper()
    data = candidate(queue, tmp_path)
    with queue._decision_gate(native=True) as (db, finalize):
        with pytest.raises(ValueError):
            retry.issue(
                db,
                data,
                queue._native,
                queue._legs,
                now={**NOW, "monotonic_ns": 2**63 - 1},
                finalize=finalize,
            )
        grant = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
        for observed in (
            {**NOW, "wall_ns": 999},
            {**NOW, "wall_ns": 11000},
            {**NOW, "monotonic_ns": 12000},
            {**NOW, "boot_id": BOOT[:-1] + "2"},
        ):
            with pytest.raises(ValueError):
                retry.fresh(grant, observed, queue._native)
        retry.fresh(grant, dict(boot_id=BOOT, wall_ns=10999, monotonic_ns=11999), queue._native)


def test_revoke_is_exact_and_does_not_clear_hold_or_reset_pending(queue, tmp_path):
    retry = helper()
    data = candidate(queue, tmp_path)
    with queue._decision_gate(native=True) as (db, finalize):
        grant = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
        with pytest.raises(ValueError):
            retry.revoke(db, data["hash"], "cbm", "c" * 32, queue._native)
        assert (
            retry.revoke(db, data["hash"], "cbm", grant["nonce"], queue._native)["status"]
            == "revoked"
        )
        assert (
            retry.revoke(db, data["hash"], "cbm", grant["nonce"], queue._native)["status"]
            == "revoked"
        )
    assert {r["attempts"] for r in queue.list_markers()} == {0}


def test_corrupt_current_grant_is_preserved_instead_of_silently_overwritten(queue, tmp_path):
    retry = helper()
    data = candidate(queue, tmp_path)
    with queue._decision_gate(native=True) as (db, finalize):
        retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=finalize)
        db.execute("UPDATE provider_state SET grant=?", (b"{broken",))
    with (
        pytest.raises((ValueError, json.JSONDecodeError)),
        queue._decision_gate(native=True) as (db, finalize),
    ):
        retry.issue(
            db, {**data, "nonce": "c" * 32}, queue._native, queue._legs, now=NOW, finalize=finalize
        )
    with closing(queue._connect()) as db:
        assert bytes(db.execute("SELECT grant FROM provider_state").fetchone()[0]) == b"{broken"


@pytest.mark.parametrize("change", ["snapshot", "removed"])
def test_issue_selects_actual_work_after_the_last_evidence_drain(queue, tmp_path, change):
    retry = helper()
    data = candidate(queue, tmp_path)
    with queue._decision_gate(native=True) as (db, finalize):
        def changed_work():
            # Model a fresh cleanup/evidence reducer restoring or retiring work
            # on this same transaction before its final typed drain completes.
            if change == "removed":
                db.execute("DELETE FROM pending WHERE hash=? AND provider='cbm'", (data["hash"],))
            else:
                db.execute(
                    "UPDATE pending SET mode='moderate',attempts=3 WHERE hash=? AND provider='cbm'",
                    (data["hash"],),
                )
            return finalize()

        if change == "removed":
            with pytest.raises(ValueError, match="absent"):
                retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=changed_work)
            assert retry.load(db, data["hash"], "cbm", queue._native) is None
        else:
            grant = retry.issue(db, data, queue._native, queue._legs, now=NOW, finalize=changed_work)
            _, actual = retry.selected_request(db, data["hash"], "cbm", "pending", queue._native, queue._legs)
            assert grant["request"] == actual
            assert grant["request"]["attempts"] == 3
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
