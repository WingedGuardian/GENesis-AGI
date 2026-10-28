"""Initialization-lock diagnostics observe prerequisites without queue IO."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_codebase_managed_config import config
from tests.test_scripts.test_codebase_managed_config import managed as managed  # noqa: F401


@pytest.mark.parametrize("exists", [False, True], ids=["absent", "writable"])
def test_status_observes_init_lock_conditionally_without_queue_io(
    managed, tmp_path, monkeypatch, exists
):
    state = tmp_path / "owned-state"
    lock = state / "index-requests/.queue-db-init.lock"
    monkeypatch.setenv("GENESIS_HOME", str(state))
    if exists:
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b"retained lock bytes")
    before = {
        p: (p.stat().st_ino, p.stat().st_mode, p.read_bytes() if p.is_file() else None)
        for p in tmp_path.rglob("*")
    }

    def forbidden(*args, **kwargs):
        pytest.fail("status must not open a database or start a provider")

    def manager_only(command, **kwargs):
        assert command[:4] == ["/usr/bin/systemctl", "--user", "show", managed.BACKEND]
        return "LoadState=not-found\nActiveState=inactive\n"

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(managed.subprocess, "run", forbidden)
    monkeypatch.setattr(managed.subprocess, "Popen", forbidden)
    monkeypatch.setattr(managed.subprocess, "check_output", manager_only)
    result = managed.status(None, "owned missing settings")
    advisory = result["queue_initialization"]
    assert advisory["path"] == str(lock) and advisory["exists"] is exists
    assert advisory["state"] == ("observed_writable" if exists else "absent")
    assert advisory["required_when"] == "queue initialization or schema upgrade"
    assert advisory["error"] is None
    assert result["settings_error"] == "owned missing settings" and result["manager_error"] is None
    assert result["service"]["ActiveState"] == "inactive"
    assert {
        p: (p.stat().st_ino, p.stat().st_mode, p.read_bytes() if p.is_file() else None)
        for p in tmp_path.rglob("*")
    } == before
    assert not (lock.parent / "queue.sqlite3").exists()


@pytest.mark.parametrize(
    "mode,writable", [(0o600, True), (0o200, True), (0o400, False), (0o000, False)]
)
def test_init_lock_uses_effective_append_access_only(
    managed, tmp_path, monkeypatch, mode, writable
):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    lock = tmp_path / "index-requests/.queue-db-init.lock"
    lock.parent.mkdir()
    lock.write_bytes(b"retained")
    identity = lock.stat().st_ino
    lock.chmod(mode)
    try:
        if not writable and os.access(lock, os.W_OK, effective_ids=True):
            pytest.skip("effective write-denial premise defeated for the owned init lock")
        observed = managed.queue_initialization_advisory()
        assert observed["state"] == ("observed_writable" if writable else "unavailable")
        assert bool(observed["error"]) is (not writable)
        assert observed["exists"] is True and lock.stat().st_mode & 0o777 == mode
    finally:
        lock.chmod(0o600)
    assert lock.stat().st_ino == identity and lock.read_bytes() == b"retained"


@pytest.mark.parametrize("mode", [0o500, 0o600], ids=["no-write", "no-search"])
def test_absent_init_lock_observes_existing_ancestor(managed, tmp_path, monkeypatch, mode):
    parent = tmp_path / "owned-parent"
    parent.mkdir()
    state = parent / "absent-state"
    monkeypatch.setenv("GENESIS_HOME", str(state))
    parent.chmod(mode)
    try:
        if os.access(parent, os.W_OK | os.X_OK, effective_ids=True):
            pytest.skip("effective parent write/search-denial premise defeated")
        observed = managed.queue_initialization_advisory()
    finally:
        parent.chmod(0o700)
    assert observed["state"] == "unavailable" and observed["error"]
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("override", [None, ""], ids=["unset", "empty"])
def test_init_lock_default_namespace_is_home(managed, tmp_path, monkeypatch, override):
    monkeypatch.setenv("HOME", str(tmp_path))
    if override is None:
        monkeypatch.delenv("GENESIS_HOME", raising=False)
    else:
        monkeypatch.setenv("GENESIS_HOME", override)
    observed = managed.queue_initialization_advisory()
    assert observed["path"] == str(tmp_path / ".genesis/index-requests/.queue-db-init.lock")
    assert observed["state"] == "absent" and observed["exists"] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["directory", "fifo", "dangling", "linked"])
def test_init_lock_kind_and_dangling_lookup_are_observed(managed, tmp_path, monkeypatch, kind):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    lock = tmp_path / "index-requests/.queue-db-init.lock"
    lock.parent.mkdir()
    target = tmp_path / "target"
    if kind == "directory":
        lock.mkdir()
    elif kind == "fifo":
        os.mkfifo(lock)
    elif kind == "linked":
        target.write_bytes(b"retained target")
        lock.symlink_to(target)
    else:
        lock.symlink_to(target)
    before = lock.lstat()
    observed = managed.queue_initialization_advisory()
    after = lock.lstat()
    assert observed["state"] == ("observed_writable" if kind == "linked" else "unavailable")
    assert observed["exists"] is True
    assert (before.st_ino, before.st_mode, before.st_mtime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_mtime_ns,
    )
    assert target.exists() is (kind == "linked")


@pytest.mark.parametrize("failure", ["lookup", "readonly", "invalid-home"])
def test_init_lock_lookup_mount_and_invalid_namespace_are_explicit(
    managed, tmp_path, monkeypatch, failure
):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    lock = tmp_path / "index-requests/.queue-db-init.lock"
    lock.parent.mkdir()
    lock.write_bytes(b"retained")
    if failure == "lookup":
        original = Path.lstat

        def lookup_denied(path, *args, **kwargs):
            if path == lock:
                raise PermissionError("owned lookup denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", lookup_denied)
    elif failure == "readonly":
        original = os.statvfs
        monkeypatch.setattr(
            os,
            "statvfs",
            lambda path: (
                SimpleNamespace(f_flag=os.ST_RDONLY) if Path(path) == lock else original(path)
            ),
        )
    else:
        monkeypatch.setenv("GENESIS_HOME", "relative-state")
    observed = managed.queue_initialization_advisory()
    assert observed["state"] == "unavailable" and observed["error"]
    assert observed["exists"] is (True if failure == "readonly" else None)
    assert lock.read_bytes() == b"retained"


@pytest.mark.parametrize("old_schema", [False, True], ids=["current-schema", "old-schema"])
def test_init_lock_warning_does_not_redefine_actual_queue_operations(
    managed, tmp_path, monkeypatch, old_schema
):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    value = config(tmp_path, managed)
    for key in ("main", "cache", "runtime"):
        Path(value[key]).mkdir()
    (tmp_path / "locks").mkdir()
    marker = Path(__file__).resolve().parents[2] / "scripts/lib/index_marker.py"
    command = ["python3", str(marker)]
    subprocess.run(
        [*command, "write", "--repo", str(tmp_path / "repo")],
        env=dict(os.environ),
        check=True,
        capture_output=True,
        timeout=15,
    )
    queue = tmp_path / "index-requests"
    if old_schema:
        with sqlite3.connect(queue / "queue.sqlite3") as db:
            db.execute("PRAGMA user_version=0")
    lock = queue / ".queue-db-init.lock"
    lock.chmod(0o400)
    try:
        if os.access(lock, os.W_OK, effective_ids=True):
            pytest.skip("effective write-denial premise defeated for the owned init lock")
        advisory = managed.queue_initialization_advisory()
        assert advisory["state"] == "unavailable" and advisory["error"]
        if not old_schema:
            managed.verify_worker_writes(value, False)
        result = subprocess.run(
            [*command, "list"], env=dict(os.environ), capture_output=True, text=True, timeout=15
        )
    finally:
        lock.chmod(0o600)
    assert (result.returncode != 0) is old_schema
    if old_schema:
        assert "PermissionError" in result.stderr and str(lock) in result.stderr
    else:
        assert result.stdout.strip().split("\t")[4] == "0"


def test_init_lock_advisory_preserves_settings_manager_and_availability(
    managed, tmp_path, monkeypatch
):
    value = config(tmp_path, managed)
    for key in ("main", "cache", "runtime"):
        Path(value[key]).mkdir()
    state = tmp_path / "state"
    (state / "locks").mkdir(parents=True)
    lock = state / "index-requests/.queue-db-init.lock"
    lock.parent.mkdir()
    lock.write_bytes(b"retained")
    monkeypatch.setenv("GENESIS_HOME", str(state))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(value))
    check = managed.queue_initialization_advisory.__globals__["_writable_path"]

    def denied_init(path, **kwargs):
        if path == lock:
            raise PermissionError("owned initialization denial")
        return check(path, **kwargs)

    monkeypatch.setitem(
        managed.queue_initialization_advisory.__globals__, "_writable_path", denied_init
    )
    monkeypatch.setattr(
        managed.subprocess, "check_output", lambda *a, **kw: "ActiveState=inactive\n"
    )
    observed = managed.status(path)
    assert observed["queue_initialization"]["state"] == "unavailable"
    assert observed["settings"] == value and observed["settings_error"] is None
    assert observed["service"] == {"ActiveState": "inactive"} and observed["manager_error"] is None
    managed.verify_worker_writes(value, False)

    def manager_unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(managed.subprocess, "check_output", manager_unavailable)
    observed = managed.status(path)
    assert observed["manager_error"] and observed["queue_initialization"]["error"]
    assert observed["settings_error"] is None and lock.read_bytes() == b"retained"
