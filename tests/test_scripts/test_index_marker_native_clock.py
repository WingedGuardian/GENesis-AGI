"""Finite native receipt metadata cannot poison immutable clock settlement."""

import json
import sqlite3
from contextlib import closing

import pytest

from tests.test_scripts.test_index_marker_execution import (
    _begin,
    _claim,
    _register,
    _reservation,
    _reserve,
)
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401
from tests.test_scripts.test_index_marker_native_replay import _apply, _cleanup, _event, _terminal


@pytest.mark.parametrize("route", ["direct", "spool", "retained-db"])
@pytest.mark.parametrize("effect", ["full-success", "escalated-failure", "ordinary-failure"])
@pytest.mark.parametrize("timestamp", [1000, 2**63, 10**30, 1e30])
def test_finite_receipt_metadata_settles_each_clock_without_rewriting_retained_evidence(
    queue, tmp_path, monkeypatch, route, effect, timestamp
):
    claimed = _claim(queue, tmp_path, mode="full" if effect == "full-success" else "fast")
    reservation = _reservation(claimed)
    if effect == "escalated-failure":
        reservation["run_mode"] = "full"
    _reserve(queue, claimed, reservation)
    _register(queue, claimed)
    _begin(queue, claimed)
    terminal = _terminal(claimed, rc=0 if effect == "full-success" else 1)
    retained = None
    monkeypatch.setattr(queue, "OUTCOME_LOCK_WAIT_S", 0.02)
    if route == "retained-db":
        # Accepted pre-fix writer representation: canonical event with integer
        # metadata, already immutable in SQLite and no longer passing event().
        event = dict(version=3, kind="terminal", context=claimed["context"],
                     data=terminal, recorded_at=timestamp)
        with queue._queue_gate() as db:
            assert queue._native.record(db, claimed["context"], event, queue._legs, queue._proof) == "recorded"
        retained = queue._native.encode(event)
    elif route == "spool":
        with closing(sqlite3.connect(queue.database_path())) as db:
            db.execute("BEGIN EXCLUSIVE")
            path = _event(queue, claimed, "terminal", terminal, at=timestamp)
            assert path.is_file()
            db.rollback()
    else:
        _event(queue, claimed, "terminal", terminal, at=timestamp)
    expected = "consumed" if effect == "full-success" else "pending"
    assert _apply(queue, claimed) == expected
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    assert _apply(queue, claimed) == expected
    with closing(sqlite3.connect(queue.database_path())) as db:
        full, backoff = db.execute("SELECT last_full,full_backoff FROM repo_state WHERE hash=?",
                                   (claimed["context"]["hash"],)).fetchone()
        raw = db.execute("SELECT terminal FROM executions WHERE execution_seq=?",
                         (claimed["context"]["execution_seq"],)).fetchone()[0]
    assert full == (float(timestamp) if effect == "full-success" else None)
    assert backoff == (float(timestamp) if effect == "escalated-failure" else None)
    assert queue.execution_status(claimed["context"])["typed_charged_attempts"] == int(effect == "ordinary-failure")
    if retained is not None:
        assert raw == retained
    else:
        assert type(json.loads(raw)["recorded_at"]) is float


@pytest.mark.parametrize("timestamp", [True, "1000", -1, float("nan"), float("inf"), 10**1000])
def test_invalid_receipt_domain_is_refused_before_any_terminal_publication(queue, tmp_path, timestamp):
    claimed = _claim(queue, tmp_path)
    with pytest.raises(ValueError):
        _event(queue, claimed, "terminal", _terminal(claimed), at=timestamp)
    assert queue.execution_status(claimed["context"])["terminal"] is None
    assert not list(queue.marker_dir().glob(".execution-spool-*.spool"))
