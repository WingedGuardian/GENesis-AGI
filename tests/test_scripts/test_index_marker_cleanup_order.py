"""Observation order governs ownership even when immutable evidence slots fill."""

import sqlite3
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_execution import _registration
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401
from tests.test_scripts.test_index_marker_native_replay import (
    _apply,
    _cleanup,
    _event,
    _started,
    _terminal,
)


def _remember(queue, claimed, kind, data):
    try:
        return _event(queue, claimed, kind, data)
    except RuntimeError as exc:
        assert "conflict" in str(exc)  # Expected immutable-slot conflict; no other error is accepted.


def _occupy(queue, claimed, slots):
    if slots != "free":
        _remember(queue, claimed, "cleanup", {**_cleanup(queue, claimed), "state": "unresolved",
                                            "owned_wait": False, "observed_ns": 210})
    if slots == "both":
        _remember(queue, claimed, "cleanup", {**_cleanup(queue, claimed), "state": "unresolved",
                                            "owned_wait": False, "observed_ns": 220})


def _proof(queue, claimed, state):
    registered = _registration(claimed)
    if state == "quiescent":
        return _cleanup(queue, claimed)
    common = {"state": state, "registration_sha256": queue._native.digest(registered)}
    if state == "removed":
        return {**common, "root": registered["facts"]["root"], "mounts_sha256": registered["facts"]["mounts_sha256"],
                "boot_id": registered["facts"]["reporter"]["boot_id"], "absent": True, "observed_ns": 300}
    return {**common, "old_boot_id": registered["facts"]["reporter"]["boot_id"],
            "new_boot_id": "00000000-0000-0000-0000-000000000002", "observed_ns": 100}


@pytest.mark.parametrize("kind", ["cleanup", "cleanup-disproof"])
@pytest.mark.parametrize("arrival", ["proof-first", "population-first"])
@pytest.mark.parametrize("state", ["quiescent", "removed", "rebooted"])
@pytest.mark.parametrize("observed_ns,after_proof", [(250, False), (400, True)])
@pytest.mark.parametrize("slots", ["free", "failure", "both"])
def test_all_retained_population_observations_participate_in_cleanup_validity(
    queue, tmp_path, monkeypatch, kind, arrival, state, observed_ns, after_proof, slots
):
    # This table tests retained ordering with no new original-kernel proof.
    monkeypatch.setitem(queue.claim_native.__globals__, "_fresh_cleanup", lambda db, row: False)
    claimed = _started(queue, tmp_path)
    _event(queue, claimed, "terminal", _terminal(claimed))
    _occupy(queue, claimed, slots)
    population = {**_cleanup(queue, claimed), "populated": 1, "observed_ns": observed_ns}
    if kind == "cleanup":
        population["state"] = "unresolved"
    events = [("cleanup", _proof(queue, claimed, state)), (kind, population)]
    if arrival == "population-first":
        events.reverse()
    for event_kind, data in events:
        _remember(queue, claimed, event_kind, data)
    assert _apply(queue, claimed) == "consumed"
    status = queue.execution_status(claimed["context"])
    invalidated = after_proof and state != "rebooted"
    assert status["cleanup_invalidated"] == int(invalidated)
    sibling = queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert (sibling is None) == invalidated


def test_one_drain_interprets_retained_hazard_against_later_spooled_proof(queue, tmp_path, monkeypatch):
    monkeypatch.setitem(queue.claim_native.__globals__, "_fresh_cleanup", lambda db, row: False)
    claimed = _started(queue, tmp_path)
    _event(queue, claimed, "terminal", _terminal(claimed))
    _occupy(queue, claimed, "both")
    before = queue.execution_status(claimed["context"])
    monkeypatch.setattr(queue, "OUTCOME_LOCK_WAIT_S", 0.02)
    nonces = iter(("0" * 32, "f" * 32))
    monkeypatch.setattr(queue.secrets, "token_hex", lambda count: next(nonces))
    with closing(sqlite3.connect(queue.database_path())) as db:
        db.execute("BEGIN EXCLUSIVE")
        population = {**_cleanup(queue, claimed), "state": "unresolved", "populated": 1, "observed_ns": 400}
        hazard_path = _event(queue, claimed, "cleanup", population)
        proof_path = _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
        assert hazard_path.name < proof_path.name
        db.rollback()
    assert _apply(queue, claimed) == "consumed"
    after = queue.execution_status(claimed["context"])
    assert after["cleanup_invalidated"] == 1
    assert queue.get_inflight(claimed["context"]["hash"]) is not None
    assert after["cleanup_first_failure"] == before["cleanup_first_failure"]
    assert after["first_typed_conflict"] == before["first_typed_conflict"]
    assert hazard_path.is_file() and not proof_path.exists()
