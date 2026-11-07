"""Actual scratch publication retains startup facts without inventing registration."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy

import pytest

from tests.test_scripts.test_index_marker_control import (
    _closure,
    _launched,
    _physical_end,
    _publish,
    _reserved,
    _revision,
)
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _registration
from tests.test_scripts.test_index_marker_legs import SCRIPT


def _early(queue, tmp_path, *, before=0, after=0, registered=False):
    claimed, reservation = _reserved(queue, tmp_path)
    scope = _registration(claimed)
    scope["facts"]["events"]["oom_kill"] = before
    if registered:
        assert queue.register_scope(claimed["context"], scope) == "registered"
    queue.cancel_launch(claimed["context"])
    closure = _closure(queue, reservation, state="uncertain", reason="startup_incomplete")
    identity = scope["facts"]["reporter"]
    closure["foreground"] = {"identity": identity.copy(), "returncode": 125}
    closure["monitor"] = {"identity": {**identity, "pid": identity["pid"] + 1}, "returncode": 0}
    facts = deepcopy(scope["facts"])
    facts["events"]["oom_kill"] = after
    closure["startup"] = {"scope": scope, "facts": facts, "observed_ns": closure["observed_ns"] - 1}
    return claimed, closure


@pytest.mark.parametrize("before,after,ancestor,expected", [
    (0, 0, 0, "uncertainty"), (0, 1, 0, "resource"),
    (1, 1, 0, "resource"), (1, 2, 0, "resource"), (0, 0, 9, "uncertainty"),
])
@pytest.mark.parametrize("registered", [False, True])
def test_original_startup_oom_is_sticky_including_first_sample(queue, tmp_path, before, after, ancestor, expected, registered):
    claimed, closure = _early(queue, tmp_path, before=before, after=after, registered=registered)
    for item in closure["startup"]["facts"]["ancestors"]:
        item["events"]["oom_kill"] += ancestor
    _publish(queue, claimed, closure)
    status = queue.execution_status(claimed["context"])
    assert status["safety_kind"] == expected
    assert status["control_closure"]["data"] == closure
    assert (status["scope_registration"] is not None) == registered
    assert status["native_intent"] is None and status["terminal"] is None
    revision = _revision(queue, claimed)
    _publish(queue, claimed, closure, recorded_at=20)
    assert _revision(queue, claimed) == revision
    assert {row["attempts"] for row in queue.list_markers()} == {0}


def test_monitor_creation_failure_retains_absence_and_actual_foreground_wait(queue, tmp_path):
    claimed, closure = _early(queue, tmp_path)
    closure["monitor"] = None
    _publish(queue, claimed, closure)
    status = queue.execution_status(claimed["context"])
    assert status["control_closure"]["data"]["monitor"] is None
    assert status["control_closure"]["data"]["foreground"]["returncode"] == 125
    assert status["safety_kind"] == "uncertainty"


def test_busy_startup_publication_replays_without_resend_or_live_old_publisher(queue, tmp_path, monkeypatch):
    claimed, closure = _early(queue, tmp_path, before=1, after=2)
    with closing(sqlite3.connect(queue.database_path(), isolation_level=None)) as held:
        held.execute("BEGIN IMMEDIATE")
        path = _publish(queue, claimed, closure)
        assert path.suffix == ".spool" and json.loads(path.read_bytes())["data"] == closure
        held.rollback()
    monkeypatch.setattr(queue._recovery, "owner_identity", lambda: pytest.fail("replay must not re-attest old publisher"))
    queue.reconcile_native()
    status = queue.execution_status(claimed["context"])
    assert status["control_closure"]["data"] == closure and status["safety_kind"] == "resource"
    assert status["scope_registration"] is None and status["native_intent"] is None and status["terminal"] is None
    assert queue.get_inflight(claimed["hash"]) is None and not path.exists()
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "execution-status", "--context",
                             json.dumps(claimed["context"])], env=dict(os.environ),
                            capture_output=True, text=True, check=True, timeout=20)
    observed = json.loads(result.stdout)
    assert observed["control_closure"]["data"] == closure and observed["safety_kind"] == "resource"
    assert queue.claim_native(claimed["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None
    assert {row["attempts"] for row in queue.list_markers()} == {0}


def test_known_native_success_and_clean_control_cannot_erase_original_startup_oom(queue, tmp_path):
    claimed, reservation, registered, closure = _launched(queue, tmp_path)
    closure["foreground"]["identity"] = registered["facts"]["reporter"].copy()
    facts = deepcopy(registered["facts"])
    facts["events"]["oom_kill"] = 1
    closure["startup"] = {"scope": registered, "facts": facts, "observed_ns": closure["observed_ns"] - 1}
    _publish(queue, claimed, closure)
    _physical_end(queue, claimed, registered)
    assert queue.apply_native(claimed["context"]) == "consumed"
    status = queue.execution_status(claimed["context"])
    assert status["completion_kind"] == "success" and status["safety_kind"] == "resource"
    assert status["control_closure"]["data"]["state"] == "clean"
    assert queue.get_inflight(claimed["hash"]) is None
    assert queue.claim_native(claimed["hash"], provider="cbm") is None
    assert queue.claim_native(claimed["hash"], provider="gitnexus") is not None


def test_startup_uncertainty_cannot_relabel_a_committed_native_intent(queue, tmp_path):
    claimed, _, _, closure = _launched(queue, tmp_path)
    closure.update(state="uncertain", reason="startup_incomplete")
    with pytest.raises(ValueError, match="no native intent"):
        _publish(queue, claimed, closure)
    status = queue.execution_status(claimed["context"])
    assert status["launch_permission"] == "spent" and status["control_closure"] is None


def test_unregistered_children_never_become_clean_from_startup_facts(queue, tmp_path):
    claimed, closure = _early(queue, tmp_path)
    closure.update(state="clean", reason="normal")
    closure["foreground"]["returncode"] = 0
    with pytest.raises(ValueError):
        _publish(queue, claimed, closure)
    assert queue.execution_status(claimed["context"])["control_closure"] is None


@pytest.mark.parametrize("initial", ["clean", "uncertain"])
def test_late_conflicting_startup_oom_reduces_resource_and_retains_whole_body(queue, tmp_path, initial):
    claimed, changed = _early(queue, tmp_path, before=1, after=1)
    status = queue.execution_status(claimed["context"])
    first = _closure(queue, status["launch_reservation"], state=initial,
                     reason="normal" if initial == "clean" else "monitor_failed")
    _publish(queue, claimed, first)
    revision = _revision(queue, claimed)
    changed["observed_ns"] = first["observed_ns"] + 1
    for stamp in (20, 30):
        with pytest.raises(RuntimeError, match="conflict"):
            _publish(queue, claimed, changed, recorded_at=stamp)
        status = queue.execution_status(claimed["context"])
        assert status["safety_kind"] == "resource"
        assert status["control_closure"]["data"] == first
        assert _revision(queue, claimed) == revision + 1
        retained = [status["first_typed_conflict"]]
        retained.extend(json.loads(path.read_bytes()) for path in queue.marker_dir().glob(".execution-spool-*.spool"))
        assert any(item and item["data"] == changed for item in retained)


@pytest.mark.parametrize("fault", [
    "unknown-startup", "missing-scope", "missing-facts", "missing-clock", "null-startup",
    "clock-bool", "clock-future", "clock-zero", "scope-extra", "scope-budget", "scope-unit",
    "scope-invocation", "scope-boot", "child", "facts-extra", "facts-leaf", "facts-mount",
    "facts-boot", "facts-cap", "counter-reset", "no-foreground", "permission-open",
    "registered-invocation", "registered-baseline", "foreign-observer",
])
def test_startup_closed_shape_and_original_binding_refuse_before_spooling(queue, tmp_path, fault):
    registered = fault.startswith("registered-")
    claimed, closure = _early(queue, tmp_path, before=1, after=1, registered=registered)
    startup = closure["startup"]
    scope, facts = startup["scope"], startup["facts"]
    if fault == "unknown-startup":
        startup["extra"] = 1
    elif fault.startswith("missing-"):
        del startup[{"missing-scope": "scope", "missing-facts": "facts", "missing-clock": "observed_ns"}[fault]]
    elif fault == "null-startup":
        closure["startup"] = None
    elif fault.startswith("clock-"):
        startup["observed_ns"] = {"clock-bool": True, "clock-zero": 0,
                                  "clock-future": closure["observed_ns"] + 1}[fault]
    elif fault == "scope-extra":
        scope["extra"] = 1
    elif fault == "scope-budget":
        scope["stop_usec"] = 2**63
    elif fault == "scope-unit":
        scope["unit"] += "-foreign"
    elif fault in ("scope-invocation", "registered-invocation"):
        scope["invocation_id"] = "malformed" if fault == "scope-invocation" else "2" * 32
    elif fault == "scope-boot":
        scope["facts"]["reporter"]["boot_id"] = "00000000-0000-0000-0000-000000000001"
    elif fault == "child":
        closure["foreground"]["identity"]["pid"] += 100
    elif fault == "facts-extra":
        facts["extra"] = 1
    elif fault == "facts-leaf":
        facts["leaf"]["inode"] += 1
    elif fault == "facts-mount":
        facts["mounts_sha256"] = "f" * 64
    elif fault == "facts-boot":
        facts["reporter"]["boot_id"] = "00000000-0000-0000-0000-000000000001"
    elif fault == "facts-cap":
        facts["memory_max"] //= 2
    elif fault == "counter-reset":
        facts["events"]["oom_kill"] = 0
    elif fault == "no-foreground":
        closure["foreground"] = None
    elif fault == "permission-open":
        with queue._connect() as db:
            db.execute("UPDATE executions SET launch_permission='reserved' WHERE execution_seq=?",
                       (claimed["context"]["execution_seq"],))
    elif fault == "registered-baseline":
        scope["facts"]["events"]["oom_kill"] += 1
        facts["events"]["oom_kill"] += 1
    elif fault == "foreign-observer":
        closure["observer"]["pid"] += 100
    with pytest.raises(ValueError):
        _publish(queue, claimed, closure)
    status = queue.execution_status(claimed["context"])
    assert status["control_closure"] is None and status["safety_kind"] is None
    assert not list(queue.marker_dir().glob(".execution-spool-*.spool"))
