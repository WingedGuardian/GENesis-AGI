"""Actual scratch queue/publication/replay; no scope manager or provider runs."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts import test_index_marker_legs
from tests.test_scripts.test_index_marker_execution import _intent, _registration, _reservation
from tests.test_scripts.test_index_marker_native_replay import _terminal

base_queue = test_index_marker_legs.queue


@pytest.fixture
def queue(base_queue):
    base_queue._prepare()
    with closing(base_queue._connect()) as db, base_queue._transaction(db):
        base_queue._recovery.upgrade(db, base_queue._legs, base_queue._native)
    return base_queue


def _observer():
    stat = Path("/proc/self/stat").read_text().rpartition(")")[2].split()
    return {"pid": os.getpid(), "start_ticks": int(stat[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def _reserved(queue, tmp_path, *, mode="fast"):
    repo = str(tmp_path / "repo with space 雪")
    queue.write_marker(repo, "both", mode)
    claimed = queue.claim_native(queue.marker_hash(repo), provider="cbm")
    reservation = {**_reservation(claimed), "owner": _observer()}
    assert queue.reserve_launch(claimed["context"], reservation) == "reserved"
    return claimed, reservation


def _closure(queue, reservation, *, state="clean", reason="normal"):
    return {"state": state, "reason": reason, "reservation_sha256": queue._native.digest(reservation),
            "observer": _observer(), "observed_ns": time.monotonic_ns(),
            "foreground": None, "monitor": None, "stop": None}


def _publish(queue, claimed, closure, **kwargs):
    return queue.remember_native_event(claimed["context"], "control-closure", closure, **kwargs)


def _launched(queue, tmp_path, *, mode="fast"):
    claimed, reservation = _reserved(queue, tmp_path, mode=mode)
    registration = _registration(claimed)
    registration["facts"]["reporter"]["boot_id"] = reservation["owner"]["boot_id"]
    assert queue.register_scope(claimed["context"], registration) == "registered"
    intent = {**_intent(queue, claimed), "registration_sha256": queue._native.digest(registration),
              "facts": deepcopy(registration["facts"])}
    assert queue.begin_native(claimed["context"], intent) == "started"
    terminal = _terminal(claimed)
    for name in ("before", "after"):
        terminal["report"][name]["reporter"]["boot_id"] = reservation["owner"]["boot_id"]
    queue.remember_native_event(claimed["context"], "terminal", terminal)
    closure = _closure(queue, reservation)
    closure["foreground"] = {"identity": {**_observer(), "pid": 1234}, "returncode": 0}
    closure["monitor"] = {"identity": {**_observer(), "pid": 1235}, "returncode": 0}
    return claimed, reservation, registration, closure


def _physical_end(queue, claimed, registration):
    queue.remember_native_event(claimed["context"], "cleanup", {
        "state": "quiescent", "registration_sha256": queue._native.digest(registration),
        "facts": deepcopy(registration["facts"]), "populated": 0, "owned_wait": True,
        "observed_ns": time.monotonic_ns(),
    })


def _revision(queue, claimed):
    with closing(queue._connect()) as db:
        row = db.execute("SELECT hazard_revision FROM provider_state WHERE hash=? AND provider='cbm'",
                         (claimed["hash"],)).fetchone()
        return row[0] if row else 0


@pytest.mark.parametrize("kind", ["scalar", "native"])
def test_schema5_busy_before_open_keeps_supported_outcome_spooling(
    queue, tmp_path, monkeypatch, kind
):
    if kind == "native":
        claimed, reserved = _reserved(queue, tmp_path)
    else:
        repo = str(tmp_path / "scalar")
        queue.write_marker(repo, "cbm", "fast")
        claimed = queue.claim(queue.marker_hash(repo), provider="cbm")
    api = queue.remember_native_event.__globals__
    monkeypatch.setitem(api, "OUTCOME_LOCK_WAIT_S", 0.1)
    monkeypatch.setitem(api, "LOCK_WAIT_S", 0.1)
    before = queue.database_path().read_bytes()
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        try:
            if kind == "scalar":
                retained = queue.remember_outcome(claimed["context"], "consume")
            else:
                retained = queue.remember_native_event(claimed["context"], "stop", {
                    "reason": "wall", "observed_ns": time.monotonic_ns(),
                    "boot_id": reserved["owner"]["boot_id"], "unit": reserved["unit"],
                })
        finally:
            holder.rollback()
    assert retained.suffix == ".spool" and retained.is_file()
    event = json.loads(retained.read_bytes())
    context = event["context"] if kind == "native" else {key: event[key] for key in claimed["context"]}
    assert context == claimed["context"]
    assert queue.database_path().read_bytes() == before
    with closing(queue._connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5


def test_header_only_schema5_cannot_publish_control_or_release_its_writer(
    queue, tmp_path, monkeypatch
):
    claimed, reserved = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    closure = _closure(queue, reserved)
    api = queue.remember_native_event.__globals__
    monkeypatch.setitem(api, "OUTCOME_LOCK_WAIT_S", 0.1)
    before = {p.name: p.read_bytes() for p in queue.marker_dir().iterdir() if p.is_file()}
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(RuntimeError, match="readable schema5 reservation"):
                _publish(queue, claimed, closure)
        finally:
            holder.rollback()
    assert {p.name: p.read_bytes() for p in queue.marker_dir().iterdir() if p.is_file()} == before
    assert queue.execution_status(claimed["context"])["control_closure"] is None
    queue.reconcile_native()
    assert queue.get_inflight(claimed["hash"])["claim_id"] == claimed["claim_id"]
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is None


def test_cancelled_reservation_stays_owned_until_control_closes(queue, tmp_path):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    queue.reconcile_native()
    assert queue.get_inflight(claimed["hash"])["claim_id"] == claimed["claim_id"]
    closure = _closure(queue, reservation)
    _publish(queue, claimed, closure)
    queue.reconcile_native()
    assert queue.get_inflight(claimed["hash"]) is None
    assert queue.execution_status(claimed["context"])["control_closure"]["data"] == closure
    assert {row["attempts"] for row in queue.list_markers()} == {0}
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None


@pytest.mark.parametrize("lost", [("foreground",), ("monitor",), ("foreground", "monitor")])
def test_original_owner_lost_wait_closes_ended_producers_but_keeps_provider_hold(queue, tmp_path, lost):
    claimed, _, registration, closure = _launched(queue, tmp_path)
    closure.update(state="uncertain", reason="owned_wait_unavailable")
    for child in lost:
        closure[child]["returncode"] = None
    # An authenticated closure asserts the producers ended. Lost raw wait status
    # keeps provider uncertainty, independently of the closed physical/control work.
    _publish(queue, claimed, closure)
    _physical_end(queue, claimed, registration)
    assert queue.apply_native(claimed["context"]) == "consumed"
    assert queue.get_inflight(claimed["hash"]) is None
    assert queue.execution_status(claimed["context"])["safety_kind"] == "uncertainty"
    assert queue.claim_native(claimed["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None


@pytest.mark.parametrize("foreground", [0, 125, -9])
def test_launched_without_registration_retains_truthful_startup_uncertainty(queue, tmp_path, foreground):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    closure = _closure(queue, reservation, state="uncertain", reason="startup_incomplete")
    closure["foreground"] = {"identity": {**_observer(), "pid": 1234}, "returncode": foreground}
    closure["monitor"] = {"identity": {**_observer(), "pid": 1235}, "returncode": 0}
    assert _publish(queue, claimed, closure) == queue.database_path()
    status = queue.execution_status(claimed["context"])
    assert status["scope_registration"] is None and status["native_intent"] is None
    assert status["terminal"] is None and status["control_closure"]["data"] == closure
    assert status["safety_kind"] == "uncertainty"
    revision = _revision(queue, claimed)
    queue.reconcile_native()
    _publish(queue, claimed, closure)
    assert _revision(queue, claimed) == revision
    assert {row["attempts"] for row in queue.list_markers()} == {0}
    assert queue.claim_native(claimed["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None


def test_retired_historical_pointer_does_not_hide_pending_control(queue, tmp_path):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    with closing(queue._connect()) as db:
        db.execute("DELETE FROM inflight WHERE execution_seq=?", (claimed["context"]["execution_seq"],))
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is None
    _publish(queue, claimed, _closure(queue, reservation))
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None


def test_closure_replay_has_same_identity_despite_transport_timestamp(queue, tmp_path):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    closure = _closure(queue, reservation)
    _publish(queue, claimed, closure, recorded_at=10)
    _publish(queue, claimed, closure, recorded_at=20)
    with closing(queue._connect()) as db:
        assert db.execute("SELECT count(*) FROM provider_state").fetchone()[0] == 0
    assert queue.execution_status(claimed["context"])["control_closure"]["recorded_at"] == 10


@pytest.mark.parametrize("field,value", [("observer", {"pid": 123, "start_ticks": 10,
                                         "boot_id": "00000000-0000-0000-0000-000000000001"}),
                                        ("reservation_sha256", "f" * 64), ("state", "orphaned")])
def test_foreign_or_generic_orphan_closure_never_reaches_busy_spool(queue, tmp_path, field, value):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    closure = {**_closure(queue, reservation), field: value}
    if field == "state":
        closure["reason"] = "owner_died"
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as held:
        held.execute("BEGIN IMMEDIATE")
        with pytest.raises((ValueError, RuntimeError)):
            _publish(queue, claimed, closure)
        held.rollback()
    assert not list(queue.marker_dir().glob(".execution-spool-*.spool"))
    assert queue.execution_status(claimed["context"])["control_closure"] is None


def test_actual_cli_cannot_publish_another_live_owners_first_control(queue, tmp_path):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    closure = _closure(queue, reservation)
    # All declared identities and reservation fields are valid. Only the actual
    # publishing process differs, so a second shape/binding guard cannot mask it.
    result = subprocess.run([
        sys.executable, "-I", str(test_index_marker_legs.SCRIPT), "remember-native-event",
        "--context", json.dumps(claimed["context"]), "--kind", "control-closure",
        "--data", json.dumps(closure),
    ], capture_output=True, timeout=20, env=dict(os.environ))
    assert result.returncode == 2 and b"original live owner" in result.stderr
    assert not result.stdout and not list(queue.marker_dir().glob(".execution-spool-*.spool"))
    assert queue.execution_status(claimed["context"])["control_closure"] is None
    assert queue.get_inflight(claimed["hash"])["claim_id"] == claimed["claim_id"]
    assert _publish(queue, claimed, closure) == queue.database_path()
    queue.reconcile_native()
    assert queue.get_inflight(claimed["hash"]) is None


def test_busy_closure_replays_without_requiring_original_owner_alive(queue, tmp_path, monkeypatch):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    closure = _closure(queue, reservation)
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as held:
        held.execute("BEGIN IMMEDIATE")
        path = _publish(queue, claimed, closure)
        assert path.suffix == ".spool" and json.loads(path.read_bytes())["data"] == closure
        held.rollback()
    monkeypatch.setattr(queue._recovery, "owner_identity", lambda: pytest.fail("replay must not re-attest old publisher liveness"))
    queue.reconcile_native()
    assert queue.get_inflight(claimed["hash"]) is None
    assert not path.exists()
    assert queue.execution_status(claimed["context"])["control_closure"]["data"] == closure


def test_known_success_preserves_controller_uncertainty_without_recharging(queue, tmp_path):
    claimed, reservation, registration, closure = _launched(queue, tmp_path)
    closure.update(state="uncertain", reason="monitor_failed")
    closure["monitor"]["returncode"] = 1
    _publish(queue, claimed, closure)
    _physical_end(queue, claimed, registration)
    assert queue.apply_native(claimed["context"]) == "consumed"
    queue.write_marker(claimed["repo_path"], "both", "full")
    assert queue.claim_native(claimed["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None
    status = queue.execution_status(claimed["context"])
    assert status["completion_kind"] == "success" and status["safety_kind"] == "uncertainty"
    assert status["typed_charged_attempts"] == 0 and status["terminal"]["data"]["provider_ok"]
    assert _revision(queue, claimed) == 1


def test_first_control_conflict_advances_revision_once_and_retains_first_witness(queue, tmp_path):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    closure = _closure(queue, reservation)
    _publish(queue, claimed, closure)
    changed = {**closure, "observed_ns": closure["observed_ns"] + 1}
    for at in (10, 20):
        with pytest.raises(RuntimeError, match="conflict"):
            _publish(queue, claimed, changed, recorded_at=at)
        assert _revision(queue, claimed) == 1
    assert queue.execution_status(claimed["context"])["control_closure"]["data"] == closure


@pytest.mark.parametrize("child,fault", [("foreground", "boot"), ("monitor", "boot"),
                                       ("foreground", "owner"), ("monitor", "owner"),
                                       ("monitor", "other-child")])
def test_launched_closure_binds_distinct_original_boot_child_identities(queue, tmp_path, child, fault):
    claimed, reservation, registration, closure = _launched(queue, tmp_path)
    if fault == "boot":
        closure[child]["identity"]["boot_id"] = "00000000-0000-0000-0000-000000000001"
    elif fault == "owner":
        closure[child]["identity"] = _observer()
    else:
        closure[child]["identity"] = deepcopy(closure["foreground"]["identity"])
    with pytest.raises(ValueError):
        _publish(queue, claimed, closure)
    assert queue.execution_status(claimed["context"])["control_closure"] is None


def test_lost_closure_ack_replays_after_later_physical_cleanup(queue, tmp_path):
    claimed, reservation, registration, closure = _launched(queue, tmp_path)
    _publish(queue, claimed, closure, recorded_at=10)
    _physical_end(queue, claimed, registration)
    _publish(queue, claimed, closure, recorded_at=20)
    assert queue.apply_native(claimed["context"]) == "consumed"
    assert _revision(queue, claimed) == 0


def test_later_pressure_stop_keeps_resource_cause_even_after_prior_wall_stop(queue, tmp_path):
    claimed, reservation = _reserved(queue, tmp_path)
    base = {"reason": "wall", "unit": reservation["unit"],
            "observed_ns": 100, "boot_id": reservation["owner"]["boot_id"]}
    queue.remember_native_event(claimed["context"], "stop", base)
    for at in (10, 20):
        with pytest.raises(RuntimeError, match="conflict"):
            queue.remember_native_event(claimed["context"], "stop", {**base, "reason": "pressure_stop"}, recorded_at=at)
        status = queue.execution_status(claimed["context"])
        assert status["safety_kind"] == "resource"
        assert _revision(queue, claimed) == 2


def test_unrepresented_control_conflict_blocks_admission_under_drain_barrier(queue, tmp_path):
    claimed, reservation, registration, closure = _launched(queue, tmp_path)
    _publish(queue, claimed, closure)
    _physical_end(queue, claimed, registration)
    assert queue.apply_native(claimed["context"]) == "consumed"
    for offset in (1, 2):
        with pytest.raises(RuntimeError, match="conflict"):
            _publish(queue, claimed, {**closure, "observed_ns": time.monotonic_ns() + offset})
    assert list(queue.marker_dir().glob(".execution-spool-*.spool"))
    with pytest.raises(RuntimeError, match="unrepresented"):
        queue.claim_native(claimed["hash"], provider="cbm")
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None


def test_safety_observer_preserves_unowned_execution_error(queue, tmp_path):
    claimed, reservation = _reserved(queue, tmp_path)
    unknown = {**claimed["context"], "execution_seq": claimed["context"]["execution_seq"] + 1000}
    with pytest.raises((RuntimeError, ValueError)):
        queue.reserve_launch(unknown, reservation)


def test_registered_cancelled_reconciliation_records_new_uncertainty_revision_once(queue, tmp_path):
    claimed, reservation = _reserved(queue, tmp_path)
    assert queue.register_scope(claimed["context"], _registration(claimed)) == "registered"
    queue.cancel_launch(claimed["context"])
    assert _revision(queue, claimed) == 0
    for _ in range(2):
        queue.reconcile_native()
        assert queue.execution_status(claimed["context"])["safety_kind"] == "uncertainty"
        assert _revision(queue, claimed) == 1
    assert queue.get_inflight(claimed["hash"]) is not None


@pytest.mark.parametrize("fault", ["exhausted", "foreign-repository"])
def test_reconciliation_revision_failure_rolls_back_entire_settlement(queue, tmp_path, fault):
    claimed, reservation = _reserved(queue, tmp_path)
    assert queue.register_scope(claimed["context"], _registration(claimed)) == "registered"
    queue.cancel_launch(claimed["context"])
    with closing(queue._connect()) as db:
        db.execute("INSERT INTO provider_state(hash,repo_path,provider,hazard_revision) VALUES(?,?,'cbm',?)",
                   (claimed["hash"], claimed["repo_path"] if fault == "exhausted" else claimed["repo_path"] + "-foreign",
                    2**63 - 1 if fault == "exhausted" else 0))
        before = list(db.iterdump())
    with pytest.raises(RuntimeError, match="identity or revision"):
        queue.reconcile_native()
    with closing(queue._connect()) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("initial", ["clean", "uncertain"])
@pytest.mark.parametrize("prior_stop", [None, "wall", "pressure_stop"])
def test_conflicting_control_retains_embedded_pressure_stop_and_entire_receipt(queue, tmp_path, initial, prior_stop):
    claimed, reservation = _reserved(queue, tmp_path)
    queue.cancel_launch(claimed["context"])
    if prior_stop is not None:
        queue.remember_native_event(claimed["context"], "stop", {
            "unit": reservation["unit"], "boot_id": reservation["owner"]["boot_id"],
            "reason": prior_stop, "observed_ns": 100,
        })
    first = _closure(queue, reservation, state=initial, reason="normal" if initial == "clean" else "monitor_failed")
    _publish(queue, claimed, first)
    revision = _revision(queue, claimed)
    changed = {**first, "observed_ns": time.monotonic_ns(), "stop": {
        "unit": reservation["unit"], "boot_id": reservation["owner"]["boot_id"],
        "reason": "pressure_stop", "observed_ns": 200,
    }}
    for at in (10, 20):
        with pytest.raises(RuntimeError, match="conflict"):
            _publish(queue, claimed, changed, recorded_at=at)
        status = queue.execution_status(claimed["context"])
        assert status["first_stop"] is not None and status["safety_kind"] == "resource"
        assert _revision(queue, claimed) == revision + 1
        retained = [status["first_typed_conflict"]]
        retained.extend(json.loads(path.read_bytes()) for path in queue.marker_dir().glob(".execution-spool-*.spool"))
        assert any(item and item["kind"] == "control-closure" and item["data"] == changed for item in retained)
    assert queue.execution_status(claimed["context"])["control_closure"]["data"] == first
