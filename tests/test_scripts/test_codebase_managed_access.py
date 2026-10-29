"""Availability follows actual file roles without mutating retained state."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts.test_codebase_managed_config import config
from tests.test_scripts.test_codebase_managed_config import managed as managed  # noqa: F401
from tests.test_scripts.test_codebase_managed_config import (
    staged_configuration as staged_configuration,  # noqa: F401
)


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o200])
def test_delete_journal_recovery_observes_real_read_permissions(tmp_path, mode):
    database = tmp_path / "owned.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value BLOB)")
        db.executemany("INSERT INTO records VALUES (?,?)", [(i, b"old" * 4000) for i in range(32)])
    child = (
        "import os,sqlite3,sys\n"
        "db=sqlite3.connect(sys.argv[1])\n"
        "assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete'\n"
        "db.execute('PRAGMA synchronous=FULL')\n"
        "db.execute('PRAGMA cache_size=1')\n"
        "db.execute('BEGIN IMMEDIATE')\n"
        "db.execute('UPDATE records SET value=?', (b'new'*4000,))\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-I", "-c", child, str(database)], check=True, timeout=20)
    journal = Path(str(database) + "-journal")
    assert journal.stat().st_size > 512 and any(journal.read_bytes()[:8])
    original_mode = journal.stat().st_mode
    journal.chmod(mode)
    try:
        if mode != 0o600:
            denied = os.W_OK if mode == 0o400 else os.R_OK
            if os.access(journal, denied, effective_ids=True):
                pytest.skip(f"effective journal {'write' if denied == os.W_OK else 'read'}-denial premise defeated")
            with pytest.raises(sqlite3.OperationalError), sqlite3.connect(database) as db:
                db.execute("SELECT value FROM records").fetchall()
            assert journal.exists()
        else:
            with sqlite3.connect(database) as db:
                assert db.execute("SELECT value FROM records").fetchall() == [(b"old" * 4000,)] * 32
            assert not journal.exists()
    finally:
        if journal.exists():
            journal.chmod(original_mode)


@pytest.mark.parametrize("mode", [0o200, 0o400])
@pytest.mark.parametrize("role", ["cache", "queue-directory", "queue", "database", "wal", "shm",
                                 "queue-wal", "queue-shm", "queue-journal", "cache-journal"])
def test_inaccessible_mutable_state_refuses_without_writes(managed, tmp_path, monkeypatch, role, mode):
    value = config(tmp_path, managed)
    for key in ("main", "cache", "runtime"):
        Path(value[key]).mkdir()
    state = tmp_path / "state"
    queue = state / "index-requests"
    queue.mkdir(parents=True)
    monkeypatch.setenv("GENESIS_HOME", str(state))
    paths = {"cache": Path(value["cache"]), "queue-directory": queue,
             "queue": queue / "queue.sqlite3", "database": Path(value["cache"]) / "index.db",
             "wal": Path(value["cache"]) / "index.db-wal", "shm": Path(value["cache"]) / "index.db-shm",
             "queue-wal": queue / "queue.sqlite3-wal", "queue-shm": queue / "queue.sqlite3-shm",
             "queue-journal": queue / "queue.sqlite3-journal", "cache-journal": Path(value["cache"]) / "index.db-journal"}
    for path in paths.values():
        if not path.exists():
            path.write_bytes(b"retained")
    target = paths[role]
    before = {path: (path.stat().st_ino, path.stat().st_mode,
                     path.read_bytes() if path.is_file() else None) for path in tmp_path.rglob("*")}
    original_mode = target.stat().st_mode
    target.chmod(mode | 0o100 if target.is_dir() else mode)
    try:
        denied = os.R_OK if mode == 0o200 else os.W_OK
        if os.access(target, denied, effective_ids=True):
            pytest.skip(f"effective {role} {'read' if denied == os.R_OK else 'write'}-denial premise defeated")
        with pytest.raises(ValueError, match="path unavailable"):
            managed.verify_worker_writes(value, False)
    finally:
        target.chmod(original_mode)
    assert {path: (path.stat().st_ino, path.stat().st_mode,
                   path.read_bytes() if path.is_file() else None) for path in tmp_path.rglob("*")} == before


def test_verified_binary_requires_effective_execute_access(managed, tmp_path, monkeypatch):
    import hashlib

    binary = tmp_path / "owned-binary"
    binary.write_bytes(b"owned fixture bytes")
    monkeypatch.setitem(managed.verified_binary.__globals__, "BUILD", hashlib.sha256(binary.read_bytes()).hexdigest())
    original_mode = binary.stat().st_mode
    binary.chmod(0o401)  # owner can read; only other users can execute
    try:
        if os.access(binary, os.X_OK, effective_ids=True):
            pytest.skip("effective accepted-binary execute-denial premise defeated")
        with pytest.raises(ValueError, match="executable"), managed.verified_binary(binary):
            pass
    finally:
        binary.chmod(original_mode)


@pytest.mark.parametrize("mode", [0o401, 0o400])
def test_configure_copy_source_preserves_existing_mode_contract(managed, staged_configuration, mode):
    args, settings, state = staged_configuration
    source = Path(args.binary)
    original_mode = source.stat().st_mode
    source.chmod(mode)
    try:
        assert source.stat().st_mode & 0o777 == mode
        if mode == 0o400:
            with pytest.raises(ValueError, match="executable"):
                managed.configure(args, settings)
            assert not settings.exists() and not state.exists()
        else:
            # Copy-source validation uses mode bits, not effective execute access.
            # The case only discriminates that contract while effective execute
            # is denied to this process; under privilege it still runs, then
            # says it proved less.
            discriminating = not os.access(source, os.X_OK, effective_ids=True)
            managed.configure(args, settings)
            destination = state / "bin/codebase-memory-mcp"
            assert destination.read_bytes() == source.read_bytes()
            assert destination.stat().st_mode & 0o777 == 0o500
            assert os.access(destination, os.X_OK, effective_ids=True)
            assert managed.read_settings(settings)["binary"] == str(destination)
            if not discriminating:
                pytest.skip("copy-source mode-bit independence not discriminated: effective execute granted")
    finally:
        source.chmod(original_mode)


def test_append_log_and_non_enumerated_directories_do_not_require_read(managed, tmp_path, monkeypatch):
    value = config(tmp_path, managed)
    for key in ("cache", "runtime"):
        Path(value[key]).mkdir()
    state = tmp_path / "state"
    locks = state / "locks"
    locks.mkdir(parents=True)
    monkeypatch.setenv("GENESIS_HOME", str(state))
    log = state / "code-intelligence-runner.log"
    log.write_bytes(b"retained")
    log.chmod(0o200)
    for directory in (locks, Path(value["runtime"])):
        directory.chmod(0o300)
    try:
        managed.verify_worker_writes(value, False)
        assert not (state / "index-requests").exists()
    finally:
        log.chmod(0o600)
        for directory in (locks, Path(value["runtime"])):
            directory.chmod(0o700)
    assert log.read_bytes() == b"retained"


def test_future_readwrite_file_needs_only_write_search_on_existing_parent(managed, tmp_path):
    check = managed.verify_worker_writes.__globals__["_writable_path"]
    tmp_path.chmod(0o300)
    try:
        check(tmp_path / "absent" / "database", directory=False, readable=True)
    finally:
        tmp_path.chmod(0o700)
    assert not list(tmp_path.iterdir())


def test_execute_access_uses_the_accepted_inode(managed, tmp_path, monkeypatch):
    import hashlib

    binary = tmp_path / "binary"
    binary.write_bytes(b"accepted")
    binary.chmod(0o500)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"foreign")
    replacement.chmod(0o400)
    real_access = os.access
    observed = []

    def replace_before_access(path, mode, *, effective_ids):
        replacement.replace(binary)
        observed.append(Path(path).read_bytes())
        return real_access(path, mode, effective_ids=effective_ids)

    monkeypatch.setattr(os, "access", replace_before_access)
    monkeypatch.setitem(managed.verified_binary.__globals__, "BUILD", hashlib.sha256(b"accepted").hexdigest())
    with managed.verified_binary(binary) as stream:
        assert stream.read() == b"accepted"
    assert observed == [b"accepted"]
    assert binary.read_bytes() == b"foreign"
