"""Durable typed replay preserves completion independently of kernel cleanup."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy

import pytest

from tests.test_scripts.test_index_marker_execution import (
    _begin,
    _claim,
    _facts,
    _register,
    _registration,
    _reservation,
    _reserve,
    _source,
)
from tests.test_scripts.test_index_marker_legs import SCRIPT
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _started(queue, tmp_path, **kwargs):
    claimed = _claim(queue, tmp_path, **kwargs)
    _reserve(queue, claimed, _reservation(claimed))
    _register(queue, claimed)
    _begin(queue, claimed)
    return claimed


def _terminal(claimed, *, rc=0, oom=0):
    before, after = _facts(claimed), _facts(claimed)
    after["events"]["oom_kill"] = oom
    return {"source": _source(), "sealed_ns": 200, "provider_ok": rc == 0,
            "report": {"version": 1, "outcome": "complete", "payload_started": True,
                       "payload_returncode": rc, "before": before, "after": after,
                       "finality": {"threads": [123], "processes": [123], "pids_current": 1},
                       "error": None}}


def _event(queue, claimed, kind, data, *, at=1000):
    operation = getattr(queue, "remember_native_event", None)
    assert callable(operation), "native evidence must survive coordinator death and replay"
    return operation(claimed["context"], kind, data, recorded_at=at)


def _cleanup(queue, claimed, *, oom=0):
    facts = _facts(claimed)
    facts["events"]["oom_kill"] = oom
    return {"state": "quiescent", "registration_sha256": queue._native.digest(_registration(claimed)),
            "facts": facts, "populated": 0, "owned_wait": True, "observed_ns": 300}


def _apply(queue, claimed):
    operation = getattr(queue, "apply_native", None)
    assert callable(operation), "terminal facts need atomic exact-execution accounting"
    return operation(claimed["context"])


def test_success_applies_once_but_keeps_writer_pointer_until_cleanup(queue, tmp_path):
    claimed = _started(queue, tmp_path, mode="full")
    queue.write_marker(claimed["repo_path"], "cbm", "full")
    _event(queue, claimed, "terminal", _terminal(claimed))
    assert _apply(queue, claimed) == "consumed"
    assert _apply(queue, claimed) == "consumed"
    assert queue.get_inflight(claimed["context"]["hash"]) is not None
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is None
    with closing(sqlite3.connect(queue.database_path())) as db:
        assert db.execute("SELECT last_full FROM repo_state WHERE hash=?", (claimed["context"]["hash"],)).fetchone()[0] == 1000
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    assert _apply(queue, claimed) == "consumed"
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    assert {r["provider"] for r in queue.list_markers()} == {"cbm", "gitnexus"}
    assert {r["attempts"] for r in queue.list_markers()} == {0}


@pytest.mark.parametrize("rc,oom,expected,safety", [(0, 1, "pending", "resource"), (1, 0, "pending", None), (0, 0, "consumed", None)])
def test_owned_oom_overrides_zero_and_is_independent_from_ordinary_failure(queue, tmp_path, rc, oom, expected, safety):
    claimed = _started(queue, tmp_path)
    _event(queue, claimed, "terminal", _terminal(claimed, rc=rc, oom=oom))
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed, oom=oom))
    assert _apply(queue, claimed) == expected
    status = queue.execution_status(claimed["context"])
    assert status["safety_kind"] == safety
    assert status["typed_charged_attempts"] == int(expected == "pending")
    assert _apply(queue, claimed) == expected
    if oom:
        assert queue.claim_native(claimed["context"]["hash"], provider="cbm") is None
        assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is not None


def test_duplicate_receipt_timestamp_is_not_a_conflicting_terminal(queue, tmp_path):
    claimed = _started(queue, tmp_path)
    _event(queue, claimed, "terminal", _terminal(claimed), at=1000)
    _event(queue, claimed, "terminal", _terminal(claimed), at=2000)
    status = queue.execution_status(claimed["context"])
    assert status["first_typed_conflict"] is None
    assert status["terminal"]["recorded_at"] == 1000


def test_late_disproof_never_refunds_or_reapplies_completed_work(queue, tmp_path):
    claimed = _started(queue, tmp_path, mode="full")
    original = _terminal(claimed)
    _event(queue, claimed, "terminal", original)
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    assert _apply(queue, claimed) == "consumed"
    second = queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    disproof = {**_cleanup(queue, claimed), "populated": 1, "observed_ns": 400}
    _event(queue, claimed, "cleanup-disproof", disproof)
    assert _apply(queue, claimed) == "consumed"
    status = queue.execution_status(claimed["context"])
    assert status["cleanup_invalidated"] == 1
    assert status["terminal"]["data"] == original
    assert queue.get_inflight(claimed["context"]["hash"])["claim_id"] == second["context"]["claim_id"]
    queue.cancel_launch(second["context"])
    queue.reconcile_native()
    queue.write_marker(claimed["repo_path"], "both", "full")
    assert queue.claim_native(claimed["context"]["hash"], provider="gitnexus") is None


@pytest.mark.parametrize("stop_ns,expected", [(150, "pending"), (250, "consumed")])
def test_stop_order_uses_observation_time_instead_of_receipt_order(queue, tmp_path, stop_ns, expected):
    claimed = _started(queue, tmp_path)
    _event(queue, claimed, "terminal", _terminal(claimed), at=2000)
    _event(queue, claimed, "stop", {"reason": "pressure_stop", "observed_ns": stop_ns,
                                  "boot_id": _reservation(claimed)["owner"]["boot_id"],
                                  "unit": _reservation(claimed)["unit"]}, at=1000)
    assert _apply(queue, claimed) == expected
    assert queue.execution_status(claimed["context"])["safety_kind"] == "resource"


def test_busy_terminal_fallback_is_replayed_before_next_gate(queue, tmp_path, monkeypatch):
    claimed = _started(queue, tmp_path)
    monkeypatch.setattr(queue, "OUTCOME_LOCK_WAIT_S", 0.02)
    with closing(sqlite3.connect(queue.database_path())) as db:
        db.execute("BEGIN EXCLUSIVE")
        path = _event(queue, claimed, "terminal", _terminal(claimed))
        assert path.is_file() and path.suffix == ".spool"
        original = path.read_bytes()
        assert json.loads(original)["context"] == claimed["context"]
        db.rollback()
    assert _apply(queue, claimed) == "consumed"
    assert not path.exists()
    assert queue.execution_status(claimed["context"])["terminal"]["data"] == _terminal(claimed)


def test_conflicting_terminal_retains_first_and_later_distinct_spools(queue, tmp_path):
    claimed = _started(queue, tmp_path)
    _event(queue, claimed, "terminal", _terminal(claimed))
    for rc in (1, 2):
        with pytest.raises(RuntimeError):
            _event(queue, claimed, "terminal", _terminal(claimed, rc=rc))
    status = queue.execution_status(claimed["context"])
    assert status["terminal"]["data"] == _terminal(claimed)
    assert status["first_typed_conflict"] is not None
    assert list(queue.marker_dir().glob(".execution-spool-*.spool"))


@pytest.mark.parametrize("fault", ["source", "identity", "finality", "counter-reset", "bool-version", "future-seal"])
def test_invalid_terminal_never_becomes_completion(queue, tmp_path, fault):
    claimed = _started(queue, tmp_path)
    data = deepcopy(_terminal(claimed))
    if fault == "source":
        data["source"]["commit"] = "b" * 40
    elif fault == "identity":
        data["report"]["before"]["leaf"]["inode"] += 1
    elif fault == "finality":
        data["report"]["finality"]["pids_current"] = 2
    elif fault == "counter-reset":
        data["report"]["after"]["events"]["high"] = -1
    elif fault == "bool-version":
        data["report"]["version"] = True
    else:
        data["sealed_ns"] = 2**63
    with pytest.raises((RuntimeError, ValueError)):
        _event(queue, claimed, "terminal", data)
    assert queue.execution_status(claimed["context"])["terminal"] is None


def test_late_owned_oom_in_cleanup_is_a_resource_hold_without_reaccounting(queue, tmp_path):
    claimed = _started(queue, tmp_path)
    _event(queue, claimed, "terminal", _terminal(claimed))
    _event(queue, claimed, "cleanup", _cleanup(queue, claimed))
    assert _apply(queue, claimed) == "consumed"
    with pytest.raises(RuntimeError):
        _event(queue, claimed, "cleanup", {**_cleanup(queue, claimed, oom=1), "observed_ns": 400})
    assert queue.execution_status(claimed["context"])["safety_kind"] == "resource"
    assert _apply(queue, claimed) == "consumed"


@pytest.mark.parametrize("state", ["removed", "rebooted"])
def test_original_root_removal_or_changed_boot_clears_only_ownership(queue, tmp_path, state):
    claimed = _started(queue, tmp_path)
    registered = _registration(claimed)
    data = {"state": state, "registration_sha256": queue._native.digest(registered), "observed_ns": 300}
    if state == "removed":
        data.update(root=registered["facts"]["root"], mounts_sha256=registered["facts"]["mounts_sha256"],
                    boot_id=registered["facts"]["reporter"]["boot_id"], absent=True)
    else:
        data.update(old_boot_id=registered["facts"]["reporter"]["boot_id"],
                    new_boot_id="00000000-0000-0000-0000-000000000002")
    _event(queue, claimed, "cleanup", data)
    assert _apply(queue, claimed) == "pending"
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    assert queue.execution_status(claimed["context"])["safety_kind"] == "uncertainty"


def test_actual_cli_journal_e2e_never_reissues_permit_or_replays_native_work(queue, tmp_path):
    claimed = _claim(queue, tmp_path, mode="full")
    context = json.dumps(claimed["context"])

    def run(command, data=None, **extra):
        args = [sys.executable, "-I", str(SCRIPT), command, "--context", context]
        if data is not None:
            args.extend(("--data", json.dumps(data)))
        for key, value in extra.items():
            args.extend(("--" + key, value))
        return subprocess.run(args, env=dict(os.environ), capture_output=True, timeout=20)

    for command, data, first in (("reserve-launch", _reservation(claimed), "reserved"),
                                 ("register-scope", _registration(claimed), "registered"),
                                 ("begin-native", None, "started")):
        if command == "begin-native":
            from tests.test_scripts.test_index_marker_execution import _intent
            data = _intent(queue, claimed)
        result = run(command, data)
        assert result.returncode == 0 and json.loads(result.stdout) == first, result.stderr
        assert run(command, data).returncode == 1
    assert run("remember-native-event", _terminal(claimed), kind="terminal").returncode == 0
    assert json.loads(run("apply-native").stdout) == "consumed"
    assert run("remember-native-event", _cleanup(queue, claimed), kind="cleanup").returncode == 0
    assert json.loads(run("apply-native").stdout) == "consumed"
    status = json.loads(run("execution-status").stdout)
    assert status["completion_kind"] == "success" and not status["evidence_pending"]
    assert queue.get_inflight(claimed["context"]["hash"]) is None
