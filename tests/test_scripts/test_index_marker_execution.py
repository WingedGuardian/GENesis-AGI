"""Native journal uses exact claim identity; no provider process is launched."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy

import pytest

from tests.test_scripts.test_index_marker_legs import SCRIPT
from tests.test_scripts.test_index_marker_legs import queue as queue  # noqa: F401


def _claim(queue, tmp_path, *, provider="cbm", mode="fast"):
    repo = str(tmp_path / "repo with space 雪\n")
    queue.write_marker(repo, "both", mode)
    operation = getattr(queue, "claim_native", None)
    assert callable(operation), "a typed native claim must have its own explicit facade API"
    return operation(queue.marker_hash(repo), provider=provider)


def _source():
    return {"commit": "a" * 40, "provider_digest": "b" * 64,
            "provider_version": "fixture-build", "configuration_sha256": "c" * 64,
            "files": {"scripts/owned.py": "d" * 64}}


def _reservation(claimed):
    value = claimed["context"]
    return {"unit": f"code-intel-{value['hash'][:12]}-{value['provider']}-{2**128 + int(value['claim_id'], 16)}",
            "run_mode": claimed["mode"], "source": _source(),
            "owner": {"pid": os.getpid(), "start_ticks": 1,
                      "boot_id": "00000000-0000-0000-0000-000000000001"},
            "cap_bytes": 8 * 1024**3, "route": "workload",
            "slice": "genesis-workload.slice", "wall_seconds": 3600}


def _reserve(queue, claimed, data):
    operation = getattr(queue, "reserve_launch", None)
    assert callable(operation), "launch reservation must commit before any scope can be created"
    return operation(claimed["context"], data)


def _facts(claimed):
    unit = _reservation(claimed)["unit"]
    root = {"path": "/sys/fs/cgroup", "device": 7, "inode": 1}
    parent = {"path": "/sys/fs/cgroup/genesis-workload.slice", "device": 7, "inode": 2}
    counters = {name: 0 for name in ("low", "high", "max", "oom", "oom_kill")}
    return {"version": 2, "root": root,
            "leaf": {"path": parent["path"] + "/" + unit + ".scope", "device": 7, "inode": 3},
            "reporter": {"pid": 123, "start_ticks": 10, "boot_id": _reservation(claimed)["owner"]["boot_id"]},
            "membership": f"0::/genesis-workload.slice/{unit}.scope\n",
            "mounts_sha256": "e" * 64, "memory_max": 8 * 1024**3, "swap_max": 0,
            "oom_score_adj": 0, "events": counters.copy(), "local_events": counters.copy(),
            "ancestors": [{"identity": p, "events": counters.copy(), "error": None} for p in (parent, root)]}


def _registration(claimed):
    return {"unit": _reservation(claimed)["unit"], "invocation_id": "1" * 32,
            "facts": _facts(claimed), "stop_usec": 90_000_000}


def _register(queue, claimed, data=None):
    operation = getattr(queue, "register_scope", None)
    assert callable(operation), "actual scope registration must precede all provider preparation"
    return operation(claimed["context"], data or _registration(claimed))


def _intent(queue, claimed):
    return {"source": _source(), "executable": {"device": 7, "inode": 88, "sha256": "f" * 64},
            "facts": _facts(claimed), "registration_sha256": queue._native.digest(_registration(claimed)),
            "before_ns": 100}


def _begin(queue, claimed, data=None):
    operation = getattr(queue, "begin_native", None)
    assert callable(operation), "a single committed native intent must gate the actual native spawn"
    return operation(claimed["context"], data or _intent(queue, claimed))


def test_current_schema_has_typed_execution_contract_and_still_refuses_automatic(queue):
    queue._prepare()
    with closing(sqlite3.connect(queue.database_path())) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        assert version == 4, "native journal needs schema4, not a metadata flag on schema3"
        columns = {row[1] for row in db.execute("PRAGMA table_info(executions)")}
        assert {"launch_reservation", "scope_registration", "native_intent", "terminal", "cleanup_proof", "cleanup_invalidated"} <= columns
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "automatic-ready"],
                            env=dict(os.environ), capture_output=True, timeout=20)
    assert result.returncode == 10 and json.loads(result.stdout)["automatic_protocol_ready"] == 0


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
def test_native_claim_is_separate_protocol_and_keeps_sibling(queue, tmp_path, provider):
    claimed = _claim(queue, tmp_path, provider=provider)
    context = claimed["context"]
    assert context["protocol"] == 3 and context["provider"] == provider
    assert set(context) == queue._legs.CONTEXT
    rows = queue.list_markers()
    assert len(rows) == 1 and rows[0]["provider"] != provider
    assert queue.claim(context["hash"], provider=rows[0]["provider"]) is None


@pytest.mark.parametrize("action", ["consume", "restore", "restore_failure"])
def test_scalar_actions_never_promote_native_claim_to_scalar_proof(queue, tmp_path, action):
    claimed = _claim(queue, tmp_path)
    with pytest.raises((ValueError, RuntimeError)):
        queue.remember_outcome(claimed["context"], action)
    assert queue.get_inflight(claimed["context"]["hash"])["claim_id"] == claimed["context"]["claim_id"]


def test_native_claim_with_no_launch_reservation_recovers_neutrally_once(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    queue.write_marker(claimed["repo_path"], "cbm", "full")
    recover = getattr(queue, "reconcile_native", None)
    assert callable(recover), "typed recovery must not reuse scalar no-intent reconciliation"
    recover()
    recover()
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    rows = {row["provider"]: row for row in queue.list_markers()}
    assert rows["cbm"]["attempts"] == rows["gitnexus"]["attempts"] == 0
    assert rows["cbm"]["mode"] == "full"


def test_actual_cli_native_claim_carries_complete_nul_context(queue, tmp_path):
    repo = str(tmp_path / "literal$()'\n")
    queue.write_marker(repo, "both", "fast")
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "claim-native", "--hash",
                             queue.marker_hash(repo), "--provider", "cbm", "--nul"],
                            env=dict(os.environ), capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    fields = result.stdout.split(b"\0")
    assert len(fields) == 8 and fields[-1] == b""
    assert fields[:5] == [b"1", b"claim", queue.marker_hash(repo).encode(), repo.encode(), b"cbm"]
    context = json.loads(fields[6])
    assert context["protocol"] == 3 and set(context) == queue._legs.CONTEXT


def test_reservation_replay_never_issues_a_second_launch_permit(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    data = _reservation(claimed)
    assert _reserve(queue, claimed, data) == "reserved"
    assert _reserve(queue, claimed, data) == "already_reserved"
    changed = {**data, "run_mode": "full"}
    with pytest.raises((ValueError, RuntimeError)):
        _reserve(queue, claimed, changed)
    assert queue.execution_status(claimed["context"])["launch_reservation"] == data


@pytest.mark.parametrize("field,value", [
    ("unit", "unrelated.scope"), ("unit", ""), ("run_mode", "both"),
    ("cap_bytes", True), ("cap_bytes", 0), ("cap_bytes", 2 * 1024**3),
    ("slice", "other.slice"), ("wall_seconds", True), ("wall_seconds", -1),
    ("wall_seconds", 2**63), ("owner", None), ("source", {"commit": "a" * 40}),
])
def test_invalid_reservation_does_not_publish_launch_authority(queue, tmp_path, field, value):
    claimed = _claim(queue, tmp_path)
    operation = getattr(queue, "reserve_launch", None)
    assert callable(operation), "complete native reservation API required"
    data = {**_reservation(claimed), field: value}
    with pytest.raises((ValueError, RuntimeError)):
        operation(claimed["context"], data)
    assert queue.execution_status(claimed["context"])["launch_reservation"] is None


def test_cancelled_reservation_cannot_be_reopened_by_replay(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    data = _reservation(claimed)
    assert _reserve(queue, claimed, data) == "reserved"
    assert queue.cancel_launch(claimed["context"]) == "cancelled"
    assert _reserve(queue, claimed, data) == "cancelled"
    queue.reconcile_native()
    assert queue.get_inflight(claimed["context"]["hash"]) is None
    assert {row["attempts"] for row in queue.list_markers()} == {0}


def test_reservation_binds_complete_exact_context_and_retains_original_on_conflict(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    data = _reservation(claimed)
    assert _reserve(queue, claimed, data) == "reserved"
    wrong = deepcopy(claimed["context"])
    wrong["claim_id"] = "0" * 32 if wrong["claim_id"] != "0" * 32 else "1" * 32
    with pytest.raises((ValueError, RuntimeError)):
        queue.reserve_launch(wrong, data)
    assert queue.execution_status(claimed["context"])["launch_reservation"] == data


def test_native_permission_is_spent_once_only_after_registered_scope(queue, tmp_path):
    claimed = _claim(queue, tmp_path)
    assert _reserve(queue, claimed, _reservation(claimed)) == "reserved"
    with pytest.raises((RuntimeError, ValueError)):
        _begin(queue, claimed)
    assert _register(queue, claimed) == "registered"
    assert _register(queue, claimed) == "already_registered"
    assert _begin(queue, claimed) == "started"
    assert _begin(queue, claimed) == "already_started"
    assert queue.cancel_launch(claimed["context"]) == "spent"
    assert queue.execution_status(claimed["context"])["launch_permission"] == "spent"


@pytest.mark.parametrize("fault", ["leaf", "boot", "cap", "reporter", "stop-zero", "stop-overflow", "slice"])
def test_registration_refuses_unbound_or_unobservable_scope(queue, tmp_path, fault):
    claimed = _claim(queue, tmp_path)
    _reserve(queue, claimed, _reservation(claimed))
    data = _registration(claimed)
    if fault == "leaf":
        data["facts"]["leaf"]["path"] += "-other"
    elif fault == "boot":
        data["facts"]["reporter"]["boot_id"] = "00000000-0000-0000-0000-000000000002"
    elif fault == "cap":
        data["facts"]["memory_max"] = 2 * 1024**3
    elif fault == "reporter":
        data["facts"]["oom_score_adj"] = 1000
    elif fault == "slice":
        data["facts"]["leaf"]["path"] = data["facts"]["leaf"]["path"].replace("genesis-workload.slice", "app.slice")
    else:
        data["stop_usec"] = 0 if fault == "stop-zero" else 2**63
    with pytest.raises((RuntimeError, ValueError)):
        _register(queue, claimed, data)
    assert queue.execution_status(claimed["context"])["scope_registration"] is None


@pytest.mark.parametrize("fault", ["source", "registration", "identity", "counter-reset", "preparation-oom", "cancelled"])
def test_native_intent_rechecks_bound_proof_and_current_authority(queue, tmp_path, fault):
    claimed = _claim(queue, tmp_path)
    _reserve(queue, claimed, _reservation(claimed))
    registered = _registration(claimed)
    registered["facts"]["events"]["high"] = 2
    _register(queue, claimed, registered)
    data = _intent(queue, claimed)
    data["registration_sha256"] = queue._native.digest(registered)
    data["facts"]["events"]["high"] = 2
    if fault == "source":
        data["source"]["commit"] = "b" * 40
    elif fault == "registration":
        data["registration_sha256"] = "0" * 64
    elif fault == "identity":
        data["facts"]["leaf"]["inode"] += 1
    elif fault == "counter-reset":
        data["facts"]["events"]["high"] = 1
    elif fault == "preparation-oom":
        data["facts"]["events"]["oom_kill"] = 1
    else:
        queue.cancel_launch(claimed["context"])
    with pytest.raises((RuntimeError, ValueError)):
        _begin(queue, claimed, data)
    status = queue.execution_status(claimed["context"])
    assert status["native_intent"] is None
    if fault == "preparation-oom":
        assert status["safety_kind"] == "resource"
