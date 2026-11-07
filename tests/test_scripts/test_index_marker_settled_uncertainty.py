"""Settled safety holds differ from provisional pre-application knowledge."""

import sqlite3
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_cleanup_order import _proof
from tests.test_scripts.test_index_marker_execution import (
    _claim,
    _intent,
    _register,
    _reservation,
    _reserve,
)
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401
from tests.test_scripts.test_index_marker_native_replay import _apply, _event, _started, _terminal


@pytest.mark.parametrize("state", ["quiescent", "removed", "rebooted"])
def test_settled_preparation_uncertainty_survives_physical_cleanup(queue, tmp_path, state):
    claimed = _claim(queue, tmp_path)
    _reserve(queue, claimed, _reservation(claimed))
    _register(queue, claimed)
    queue.cancel_launch(claimed["context"])
    queue.reconcile_native()
    before = queue.execution_status(claimed["context"])
    assert before["safety_kind"] == "uncertainty" and before["typed_charged_attempts"] == 0
    _event(queue, claimed, "cleanup", _proof(queue, claimed, state))
    assert _apply(queue, claimed) == "pending"
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    assert queue.execution_status(claimed["context"])["safety_kind"] == "uncertainty"
    queue.write_marker(claimed["repo_path"], "cbm", "full")
    assert queue.claim(claimed["context"]["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["context"]["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is not None


@pytest.mark.parametrize("state", ["quiescent", "removed", "rebooted"])
def test_late_known_terminal_cannot_release_an_applied_unknown(queue, tmp_path, state):
    claimed = _started(queue, tmp_path, mode="full")
    _event(queue, claimed, "cleanup", _proof(queue, claimed, state))
    assert _apply(queue, claimed) == "pending"
    before = queue.execution_status(claimed["context"])
    assert before["completion_kind"] == "unknown" and before["typed_charged_attempts"] == 1
    _event(queue, claimed, "terminal", _terminal(claimed))
    assert _apply(queue, claimed) == "pending"
    after = queue.execution_status(claimed["context"])
    assert after["completion_kind"] == "unknown" and after["safety_kind"] == "uncertainty"
    assert after["typed_charged_attempts"] == 1
    queue.write_marker(claimed["repo_path"], "cbm", "full")
    assert queue.claim(claimed["context"]["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["context"]["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is not None


def test_same_gate_cleanup_before_terminal_can_refine_provisional_uncertainty(queue, tmp_path, monkeypatch):
    claimed = _started(queue, tmp_path)
    monkeypatch.setattr(queue, "OUTCOME_LOCK_WAIT_S", 0.02)
    nonces = iter(("0" * 32, "f" * 32))
    with monkeypatch.context() as patcher, closing(sqlite3.connect(queue.database_path())) as db:
        patcher.setattr(queue.secrets, "token_hex", lambda count: next(nonces))
        db.execute("BEGIN EXCLUSIVE")
        cleanup = _event(queue, claimed, "cleanup", _proof(queue, claimed, "quiescent"))
        terminal = _event(queue, claimed, "terminal", _terminal(claimed))
        assert cleanup.name < terminal.name
        db.rollback()
    assert _apply(queue, claimed) == "consumed"
    assert queue.execution_status(claimed["context"])["safety_kind"] is None
    assert queue.begin_native(claimed["context"], _intent(queue, claimed)) == "already_started"
