"""Queue probes use immutable setup and persistent native authority read-only."""

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_scripts.test_codebase_managed_config import config, managed

availability = managed


@pytest.mark.parametrize(
    "fault",
    ["none", "missing", "malformed", "schema", "build", "sentinel", "disabled", "other-repo"],
)
def test_readonly_availability_refuses_incomplete_authority(availability, tmp_path, monkeypatch, fault):
    managed = availability
    value = config(tmp_path, managed)
    main = Path(value["main"])
    (main / ".git").mkdir(parents=True)
    for key in ("cache", "runtime"):
        Path(value[key]).mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "genesis-state"))
    namespace = managed.main.__globals__
    monkeypatch.setitem(namespace, "SCRIPT", main / "scripts/codebase_managed.py")
    monkeypatch.setitem(
        namespace,
        "show",
        lambda *args, **kwargs: {"UnitFileState": "disabled" if fault == "disabled" else "enabled"},
    )
    cache = Mock()
    monkeypatch.setitem(namespace, "verify_cache", cache)
    # Native RPC/cgroup semantics have dedicated runtime/native tests. Preserve
    # real require_enabled here while controlling only endpoint readiness.
    monkeypatch.setitem(namespace, "ready", managed.require_enabled)
    path = tmp_path / "settings.json"
    if fault == "schema":
        value["version"] = 1
    elif fault == "build":
        value["build"] = "unsupported"
    elif fault == "sentinel":
        Path(value["sentinel"]).touch()
    if fault != "missing":
        path.write_text("{" if fault == "malformed" else json.dumps(value))
    before = path.read_bytes() if path.exists() else None
    repo = tmp_path if fault == "other-repo" else main
    assert managed.main(["--config", str(path), "available", "--repo", str(repo)]) == (
        0 if fault == "none" else 1
    )
    assert (path.read_bytes() if path.exists() else None) == before
    if fault in ("missing", "malformed", "schema", "build", "other-repo"):
        cache.assert_not_called()


@pytest.mark.parametrize("persistence", [False, True])
@pytest.mark.parametrize("artifact_exists", [False, True])
def test_persistence_checks_the_actual_export_directory(availability, tmp_path, monkeypatch,
                                                      persistence, artifact_exists):
    value = config(tmp_path, availability)
    for key in ("main", "cache", "runtime"):
        Path(value[key]).mkdir()
    main = Path(value["main"])
    artifact = main / ".codebase-memory"
    if artifact_exists:
        artifact.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "state"))
    before = sorted(tmp_path.rglob("*"))
    main.chmod(0o500)
    try:
        if persistence and not artifact_exists:
            with pytest.raises(ValueError, match="ReadWritePaths"):
                availability.verify_worker_writes(value, persistence)
        else:
            availability.verify_worker_writes(value, persistence)
        assert sorted(tmp_path.rglob("*")) == before  # no locks, queue or probe writes
    finally:
        main.chmod(0o700)


@pytest.mark.parametrize("key", ["cache", "runtime", "state", "database", "wal", "shm", "artifact"])
def test_blocked_writer_is_diagnosed_readonly(availability, tmp_path, monkeypatch, key):
    value = config(tmp_path, availability)
    for name in ("main", "cache", "runtime"):
        Path(value[name]).mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(state))
    database = Path(value["cache"]) / "home-owned.db"
    database.write_bytes(b"retained database")
    wal, shm = Path(str(database) + "-wal"), Path(str(database) + "-shm")
    wal.touch()
    shm.touch()
    artifact = Path(value["main"]) / ".codebase-memory"
    artifact.mkdir()
    target = {"state": state, "database": database, "wal": wal,
              "shm": shm, "artifact": artifact}.get(key, Path(value.get(key, value["cache"])))
    target.chmod(0o500 if target.is_dir() else 0o400)
    try:
        with pytest.raises(ValueError, match="ReadWritePaths"):
            availability.verify_worker_writes(value, True)
        assert database.read_bytes() == b"retained database"
        assert not (state / "locks").exists()
    finally:
        target.chmod(0o700 if target.is_dir() else 0o600)


@pytest.mark.parametrize("fault", ["readonly-mount", "lookup-denied", "not-directory", "broken-link"])
def test_write_diagnosis_does_not_convert_errors_to_missing(availability, tmp_path, monkeypatch, fault):
    namespace = availability.verify_worker_writes.__globals__
    check = namespace["_writable_path"]
    target = tmp_path / "target"
    if fault == "readonly-mount":
        monkeypatch.setattr(os, "statvfs", lambda path: SimpleNamespace(f_flag=os.ST_RDONLY))
        error = ValueError
    elif fault == "lookup-denied":
        parent = tmp_path / "blocked"
        parent.mkdir()
        target = parent / "target"
        parent.chmod(0)
        error = PermissionError
    elif fault == "not-directory":
        target.write_text("retained")
        error = ValueError
    else:
        target.symlink_to(tmp_path / "missing")
        error = FileNotFoundError
    try:
        with pytest.raises(error):
            check(target, directory=True)
    finally:
        if fault == "lookup-denied":
            target.parent.chmod(0o700)


@pytest.mark.parametrize("persistence", ["true", "false"])
def test_available_binds_explicit_persistence(availability, tmp_path, monkeypatch, persistence):
    namespace = availability.main.__globals__
    observed = []
    monkeypatch.setitem(namespace, "available", lambda path, repo, persist: observed.append(persist))
    assert availability.main(["available", "--repo", str(tmp_path),
                              "--persistence", persistence]) == 0
    assert observed == [persistence == "true"]


def test_existing_writable_children_do_not_require_state_parent_write(availability, tmp_path, monkeypatch):
    value = config(tmp_path, availability)
    for key in ("cache", "runtime"):
        Path(value[key]).mkdir()
    state = tmp_path / "state"
    (state / "locks").mkdir(parents=True)
    (state / "index-requests").mkdir()
    (state / "code-intelligence-runner.log").touch()
    monkeypatch.setenv("GENESIS_HOME", str(state))
    state.chmod(0o500)
    try:
        availability.verify_worker_writes(value, False)
        assert not (state / "index-requests/queue.sqlite3").exists()
    finally:
        state.chmod(0o700)


def test_immutable_configuration_database_is_a_reader_surface(availability, tmp_path, monkeypatch):
    value = config(tmp_path, availability)
    for key in ("cache", "runtime"):
        Path(value[key]).mkdir()
    cache = Path(value["cache"])
    (cache / "config.json").write_text(json.dumps({"ui_enabled": False}))
    database = cache / "_config.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO config VALUES (?,?)", [(key, "false") for key in availability.DISABLED_KEYS])
    database.chmod(0o400)
    before = database.read_bytes()
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "state"))
    availability.verify_worker_writes(value, False)
    availability.verify_cache(value)
    assert database.read_bytes() == before
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()


@pytest.mark.parametrize("persistence", [False, True])
def test_snapshot_scratch_is_required_only_for_persistence(availability, tmp_path, monkeypatch,
                                                         persistence):
    value = config(tmp_path, availability)
    for key in ("main", "cache", "runtime"):
        Path(value[key]).mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "state"))
    original = os.statvfs
    monkeypatch.setattr(os, "statvfs", lambda path: (
        SimpleNamespace(f_flag=os.ST_RDONLY) if Path(path) == Path("/tmp") else original(path)
    ))
    before = sorted(tmp_path.rglob("*"))
    if persistence:
        with pytest.raises(ValueError, match="write path unavailable: /tmp"):
            availability.verify_worker_writes(value, persistence)
    else:
        availability.verify_worker_writes(value, persistence)
    assert sorted(tmp_path.rglob("*")) == before
