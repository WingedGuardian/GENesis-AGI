"""Final recovery drain sees callback publications without reopening SQLite."""

import json
import time
from contextlib import contextmanager
from copy import deepcopy

import pytest

from tests.test_scripts.test_index_marker_cleanup_order import _remember
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_native_replay import _started, _terminal


@contextmanager
def decision(queue):
    method = getattr(queue, "_decision_gate", None)
    assert callable(method), "explicit recovery requires a same-connection final evidence drain"
    with method(native=True) as value:
        yield value


def conflict(queue, claimed, returncode):
    data = _terminal(claimed)
    data["report"]["payload_returncode"] = returncode
    data["provider_ok"] = returncode == 0
    return queue._native.event(claimed["context"], "terminal", data, time.time(), queue._proof)


def retained(queue, tmp_path):
    claimed = _started(queue, tmp_path)
    _remember(queue, claimed, "terminal", _terminal(claimed))
    _remember(queue, claimed, "terminal", conflict(queue, claimed, 1)["data"])
    extra = conflict(queue, claimed, 2)
    path = queue._spool_native_event(extra)
    return claimed, extra, path


def test_initial_recovery_decision_can_observe_retained_conflict_before_any_grant(queue, tmp_path):
    claimed, extra, path = retained(queue, tmp_path)
    with pytest.raises(RuntimeError, match="unrepresented"):
        queue.claim_native(claimed["hash"], provider="cbm")
    with decision(queue) as (db, finalize):
        assert db.execute("SELECT grant FROM provider_state").fetchone()[0] is None
        result = finalize()
        expected = queue._native.digest(
            {key: extra[key] for key in ("version", "kind", "context", "data")}
        )
        assert result == ((claimed["hash"], "cbm", "terminal", expected),)
        assert path.exists()
    assert path.exists()  # No capacity, grant, acceptance or deletion is implied.


def test_callback_created_evidence_is_seen_even_without_another_revision(queue, tmp_path):
    claimed, _, first = retained(queue, tmp_path)
    with decision(queue) as (db, finalize):
        revision = db.execute("SELECT hazard_revision FROM provider_state").fetchone()[0]
        before = finalize()
        extra = conflict(queue, claimed, 3)
        second = queue._spool_native_event(extra)  # The admitted cleanup publisher seam.
        after = finalize()
        assert len(before) == 1 and len(after) == 2 and set(before) < set(after)
        assert db.execute("SELECT hazard_revision FROM provider_state").fetchone()[0] == revision
    assert first.exists() and second.exists()


def test_semantic_set_ignores_transport_time_filename_and_duplicates(queue, tmp_path):
    _, extra, first = retained(queue, tmp_path)
    second = queue._spool_native_event({**extra, "recorded_at": extra["recorded_at"] + 10})
    assert first != second
    with decision(queue) as (_, finalize):
        result = finalize()
        assert len(result) == 1
    assert first.exists() and second.exists()


def test_final_drain_uses_same_connection_and_retires_only_after_commit(
    queue, tmp_path, monkeypatch
):
    claimed = _started(queue, tmp_path)
    paths = {queue._spool_native_event(conflict(queue, claimed, 0))}
    namespace = queue.claim_native.__globals__
    original = namespace["_connect"]
    opened = []

    def connect(*args):
        db = original(*args)
        opened.append(db)
        return db

    monkeypatch.setitem(namespace, "_connect", connect)
    with decision(queue) as (db, finalize):
        assert db is opened[0] and finalize() == () and len(opened) == 1
        assert all(path.exists() for path in paths)
    assert len(opened) == 1 and not any(path.exists() for path in paths)
    with pytest.raises(RuntimeError, match="closed"):
        finalize()


@pytest.mark.parametrize("fault", ["malformed", "filename", "unbound"])
def test_new_invalid_or_unbound_body_cannot_be_hidden_by_initial_drain(queue, tmp_path, fault):
    claimed, _, _ = retained(queue, tmp_path)
    with pytest.raises((ValueError, RuntimeError, json.JSONDecodeError)), decision(queue) as (db, finalize):
        extra = deepcopy(conflict(queue, claimed, 3))
        if fault == "malformed":
            path = queue._spool_native_event(extra)
            path.write_text("{malformed")
        elif fault == "filename":
            path = queue._spool_native_event(extra)
            path.rename(path.with_name(".execution-spool-invalid.spool"))
        else:
            extra["data"]["source"]["configuration_sha256"] = "e" * 64
            queue._spool_native_event(extra)
        finalize()
        if fault == "unbound":
            row = queue._native.execution(db, claimed["context"], queue._legs)
            if row["completion_kind"] == "unbound":
                raise RuntimeError("unbound original execution prevents recovery")
    assert list(queue.marker_dir().glob(".execution-spool-*.spool"))


def test_rollback_retains_represented_artifacts_for_the_next_decision(queue, tmp_path):
    _, _, original = retained(queue, tmp_path)
    before = original.read_bytes()
    with pytest.raises(OSError, match="injected"), decision(queue) as (_, finalize):
        assert finalize()
        raise OSError("injected decision failure")
    assert original.read_bytes() == before
