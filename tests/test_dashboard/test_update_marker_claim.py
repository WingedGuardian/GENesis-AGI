"""The dashboard's update routes claim the deploy marker under update.lock (#2525).

Every shell writer of ~/.genesis/update_in_progress.pid (deploy_code_only.sh,
deploy_candidates, restore.sh) takes the marker only while holding
locks/update.lock, and update.sh holds that lock for its whole run. The routes
used to check, then unlink or write, with no lock, so a deploy that took the
marker in between lost it. These tests drive the real env.update_in_progress()
(through GENESIS_HOME) against a real live process holding the marker.
"""

from __future__ import annotations

import ast
import fcntl
import string
import subprocess
import sys
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.routes import updates

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="flock + POSIX pids")


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def w(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    home.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(home))
    script = tmp_path / "update.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    escalation = tmp_path / "escalation.txt"
    escalation.write_text("tier3_needed")
    state = tmp_path / "update_state.json"
    monkeypatch.setattr(updates, "_PID_FILE", home / "update_in_progress.pid")
    monkeypatch.setattr(updates, "_UPDATE_SCRIPT", script)
    monkeypatch.setattr(updates, "_ESCALATION_FILE", escalation)
    monkeypatch.setattr(updates, "_SUMMARY_FILE", tmp_path / "summary.txt")
    monkeypatch.setattr(updates, "_CONFLICT_FILE", tmp_path / "conflicts.json")
    monkeypatch.setattr(updates, "_STATE_FILE", state)
    return {"home": home, "marker": home / "update_in_progress.pid", "state": state}


@pytest.fixture()
def foreign():
    """A live process that is not this one: a deploy holding the marker."""
    proc = subprocess.Popen(["sleep", "60"])
    yield proc.pid
    proc.kill()
    proc.wait()


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@contextmanager
def _deploy_holds_the_lock(home):
    """What a shell deploy does: update.lock exclusive, on its own open file."""
    path = home / "locks" / "update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _lock_is_held(home) -> bool:
    path = home / "locks" / "update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


@pytest.fixture()
def starts(w):
    """Every way the routes start work, recorded, plus whether update.lock was
    held at that moment and what the live check saw."""
    seen: dict[str, object] = {"calls": [], "live_check_locked": None, "direct_locked": None}

    def live_refusal():
        seen["live_check_locked"] = _lock_is_held(w["home"])
        return None

    def supervised(pid_file):
        seen["calls"].append("supervised")
        pid_file.write_text("4242")
        return updates.jsonify({"status": "triggered"})

    def direct(pid_file):
        seen["calls"].append("direct")
        seen["direct_locked"] = _lock_is_held(w["home"])
        return updates.jsonify({"status": "triggered"})

    release = threading.Event()

    class P:
        pid = 4343
        returncode = 0

        def wait(self):
            release.wait(10)
            return 0

    def spawn(*a, **k):
        seen["calls"].append("spawn")
        return P()

    seen["release"] = release
    with (
        patch.object(updates, "_live_refusal", live_refusal),
        patch.object(updates, "_apply_supervised", supervised),
        patch.object(updates, "_apply_direct", direct),
        patch.object(updates, "_spawn_detached_cc", spawn),
    ):
        yield seen
    release.set()


def _join_reaper():
    for t in threading.enumerate():
        if t.name == "tier3-reaper":
            t.join(10)


# ── a deploy holding update.lock: the interleaving #2525 names ────────────────


@pytest.mark.parametrize("route", ["apply", "resolve"])
def test_a_deploy_holding_the_lock_keeps_its_marker_and_nothing_starts(client, w, starts, route):
    w["marker"].write_text("777777")  # the deploy's marker, whatever its pid
    with _deploy_holds_the_lock(w["home"]):
        r = client.post(f"/api/genesis/updates/{route}", json={})
    assert r.status_code == 409
    assert "update.lock" in r.get_json()["error"]
    assert w["marker"].read_text() == "777777"
    assert starts["calls"] == []


def test_dismiss_leaves_a_running_updates_state_while_it_holds_the_lock(client, w, starts):
    w["state"].write_text('{"phase": "merging"}')
    w["marker"].write_text("777777")
    with _deploy_holds_the_lock(w["home"]):
        r = client.post("/api/genesis/updates/dismiss")
    assert r.status_code == 409
    assert w["state"].exists() and w["marker"].read_text() == "777777"


# ── a live foreign holder without the lock ────────────────────────────────────


@pytest.mark.parametrize("route", ["apply", "resolve", "dismiss"])
def test_a_live_foreign_marker_survives_every_route(client, w, starts, foreign, route):
    """Regression pin: main already refused here (update_in_progress), unlocked."""
    w["marker"].write_text(str(foreign))
    r = client.post(f"/api/genesis/updates/{route}", json={})
    assert r.status_code == 409
    assert w["marker"].read_text() == str(foreign)
    assert starts["calls"] == []


def test_a_dead_marker_is_replaced_and_the_live_check_runs_under_the_lock(client, w, starts):
    w["marker"].write_text(str(_dead_pid()))
    r = client.post("/api/genesis/updates/apply", json={})
    assert r.status_code == 200, r.get_json()
    assert starts["calls"] == ["supervised"]
    assert starts["live_check_locked"] is True
    assert w["marker"].read_text() == "4242"


def test_the_direct_path_runs_update_sh_after_the_lock_is_released(client, w, starts):
    """update.sh takes update.lock with `flock -n` and would refuse if the route
    still held it."""
    r = client.post("/api/genesis/updates/apply", json={"supervised": False})
    assert r.status_code == 200, r.get_json()
    assert starts["calls"] == ["direct"]
    assert starts["live_check_locked"] is True
    assert starts["direct_locked"] is False


def test_dismiss_clears_a_dead_marker_and_the_files(client, w, starts):
    """Regression pin: the cleanup main already did, now under the lock."""
    w["marker"].write_text(str(_dead_pid()))
    w["state"].write_text('{"phase": "done"}')
    r = client.post("/api/genesis/updates/dismiss")
    assert r.status_code == 200
    assert not w["marker"].exists() and not w["state"].exists()


# ── the Tier 3 reaper ─────────────────────────────────────────────────────────


def test_resolve_writes_its_session_pid_and_the_reaper_removes_only_its_own(
    client, w, starts, foreign
):
    r = client.post("/api/genesis/updates/resolve", json={})
    assert r.status_code == 200, r.get_json()
    assert starts["live_check_locked"] is True
    assert w["marker"].read_text() == "4343"
    # The session ends; meanwhile a deploy replaced the dead pid with its own.
    w["marker"].write_text(str(foreign))
    starts["release"].set()
    _join_reaper()
    assert w["marker"].read_text() == str(foreign)


def test_the_reaper_removes_its_own_pid(client, w, starts):
    """Regression pin: the ordinary end of a Tier 3 session."""
    r = client.post("/api/genesis/updates/resolve", json={})
    assert r.status_code == 200, r.get_json()
    starts["release"].set()
    _join_reaper()
    assert not w["marker"].exists()


def test_the_reaper_leaves_the_marker_while_a_deploy_holds_the_lock(client, w, starts):
    r = client.post("/api/genesis/updates/resolve", json={})
    assert r.status_code == 200, r.get_json()
    with _deploy_holds_the_lock(w["home"]):
        starts["release"].set()
        _join_reaper()
    assert w["marker"].read_text() == "4343"


# ── the orchestrator ──────────────────────────────────────────────────────────


def _rendered_orchestrator() -> str:
    t = updates._ORCHESTRATOR_TEMPLATE
    fields = {f for _, f, _, _ in string.Formatter().parse(t) if f}
    return t.format(**{f: "x" for f in fields})


def test_the_orchestrator_never_writes_the_marker():
    """Writing a finished child's pid, then its own back, left a window in which
    the marker named a dead process: a deploy could take it, and the next write
    clobbered the deploy's."""
    tree = ast.parse(_rendered_orchestrator())
    writes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "write_text"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "PID_FILE"
    ]
    assert writes == []


def test_the_orchestrator_cleanup_removes_only_its_own_pid(tmp_path):
    [cleanup] = [
        n
        for n in ast.walk(ast.parse(_rendered_orchestrator()))
        if isinstance(n, ast.FunctionDef) and n.name == "cleanup"
    ]
    marker = tmp_path / "update_in_progress.pid"
    ns: dict = {"PID_FILE": marker, "MY_PID": 1234, "SCRIPT_FILE": tmp_path / "orch.py"}
    # Runs only the template's own cleanup function, taken from the rendered source.
    exec(compile(ast.Module(body=[cleanup], type_ignores=[]), "cleanup", "exec"), ns)  # noqa: S102
    marker.write_text("5678")
    ns["cleanup"]()
    assert marker.read_text() == "5678"
    marker.write_text("1234")
    ns["cleanup"]()
    assert not marker.exists()


# ── failure paths ─────────────────────────────────────────────────────────────


def test_an_exception_inside_the_claim_releases_the_lock(client, w, starts):
    def boom(pid_file):
        raise RuntimeError("spawn failed")

    with patch.object(updates, "_apply_supervised", boom), pytest.raises(RuntimeError):
        client.post("/api/genesis/updates/apply", json={})
    assert _lock_is_held(w["home"]) is False


def test_an_unopenable_lock_is_not_reported_as_a_running_deploy(client, w, starts):
    (w["home"] / "locks").write_text("not a directory")
    r = client.post("/api/genesis/updates/apply", json={})
    assert r.status_code == 500
    assert "Cannot open update.lock" in r.get_json()["error"]
    assert starts["calls"] == []


def test_a_failed_marker_write_stops_the_tier3_session(client, w, starts, monkeypatch):
    killed = []

    class P:
        pid = 6161

        def kill(self):
            killed.append(self.pid)

    class Unwritable(type(w["marker"])):
        def write_text(self, *a, **k):
            raise OSError("read-only")

    # Only the write fails: the claim, the dead-marker cleanup and the spawn run.
    monkeypatch.setattr(updates, "_PID_FILE", Unwritable(w["marker"]))
    monkeypatch.setattr(updates, "_spawn_detached_cc", lambda *a, **k: P())
    with pytest.raises(OSError):
        client.post("/api/genesis/updates/resolve", json={})
    assert killed == [6161]
    assert _lock_is_held(w["home"]) is False


def test_a_failed_marker_write_stops_the_orchestrator(w, tmp_path, monkeypatch):
    """A running orchestrator that no marker names is invisible to the watchdog
    and to every deploy."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(updates, "_GENESIS_DIR", tmp_path / "gd")
    killed = []

    class Proc:
        pid = 5151

        def kill(self):
            killed.append(self.pid)

    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: Proc())
    unwritable = tmp_path / "is_a_dir"
    unwritable.mkdir()
    with pytest.raises(OSError):
        updates._apply_supervised(unwritable)
    assert killed == [5151]
