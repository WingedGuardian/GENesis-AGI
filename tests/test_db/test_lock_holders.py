"""Tests for genesis.db.lock_holders: who holds a SQLite database's locks.

Pure parts (the /proc/locks parser, inode matching, offset decoding) run on
synthetic text. One integration test holds a real write lock in a child process
and checks the capture names it; it skips when the kernel's lock table is not
readable here or shows no matching line.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from genesis.db import lock_holders

SAMPLE = textwrap.dedent(
    """\
    1: POSIX  ADVISORY  WRITE 4242 00:39:9003 120 120
    2: POSIX  ADVISORY  READ 4242 00:39:9003 128 128
    2: -> POSIX  ADVISORY  WRITE 5151 00:39:9003 120 120
    3: POSIX  ADVISORY  READ 6161 00:39:9003 128 128
    4: POSIX  ADVISORY  READ 6161 00:39:9001 1073741826 1073742335
    5: FLOCK  ADVISORY  WRITE 7777 00:39:5555 0 EOF
    6: garbage line
    7: POSIX  ADVISORY  READ notapid 00:39:9003 128 128
    """
)


def test_parse_reads_holders_and_waiters_and_skips_malformed():
    rows = lock_holders.parse_proc_locks(SAMPLE)
    assert len(rows) == 6  # the two malformed lines are dropped
    writer = rows[0]
    assert writer == {
        "pid": 4242,
        "type": "POSIX",
        "access": "WRITE",
        "dev": "00:39",
        "inode": 9003,
        "start": 120,
        "end": 120,
        "waiting": False,
    }
    waiter = rows[2]
    assert waiter["pid"] == 5151 and waiter["waiting"] is True
    assert rows[5]["end"] is None  # EOF


def test_decode_shm_offsets():
    assert lock_holders.decode("shm", 120, 120) == "writer"
    assert lock_holders.decode("shm", 121, 121) == "checkpointer"
    assert lock_holders.decode("shm", 122, 122) == "recoverer"
    assert lock_holders.decode("shm", 123, 123) == "reader-mark 0"
    assert lock_holders.decode("shm", 127, 127) == "reader-mark 4"
    assert lock_holders.decode("shm", 128, 128) == "open (DMS)"
    assert lock_holders.decode("shm", 123, 127) == "shm bytes 123-127"
    assert lock_holders.decode("main", 1073741826, 1073742335) == "main-db"
    assert lock_holders.decode("wal", 0, None) == "wal"


def _db_files(tmp_path: Path) -> Path:
    db = tmp_path / "x.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").write_bytes(b"")
    return db


def _locks_for(db: Path, extra: str = "") -> str:
    shm = os.stat(f"{db}-shm").st_ino
    main = os.stat(db).st_ino
    # A device field that does NOT match st_dev, as measured on an overlay /
    # container filesystem: the match must be by inode alone.
    return (
        f"1: POSIX  ADVISORY  WRITE 4242 ff:ff:{shm} 120 120\n"
        f"2: POSIX  ADVISORY  READ {os.getpid()} ff:ff:{shm} 128 128\n"
        f"3: -> POSIX  ADVISORY  WRITE 5151 ff:ff:{shm} 120 120\n"
        f"4: POSIX  ADVISORY  READ 6161 ff:ff:{main} 1073741826 1073742335\n"
        "5: POSIX  ADVISORY  WRITE 8888 ff:ff:1 120 120\n" + extra
    )


def test_holders_match_by_inode_and_flag_device_mismatch(tmp_path):
    db = _db_files(tmp_path)
    result = lock_holders.holders_for(db, _locks_for(db), label=lambda pid: f"proc{pid}")
    assert result["device_mismatch"] is True
    by_pid = {(h["pid"], h["lock"], h["waiting"]) for h in result["holders"]}
    assert (4242, "writer", False) in by_pid
    assert (5151, "writer", True) in by_pid
    assert (6161, "main-db", False) in by_pid
    assert all(h["pid"] != 8888 for h in result["holders"])  # unrelated inode
    writer = next(h for h in result["holders"] if h["pid"] == 4242 and not h["waiting"])
    assert writer == {
        "pid": 4242,
        "label": "proc4242",
        "self": False,
        "file": "shm",
        "type": "POSIX",
        "access": "WRITE",
        "lock": "writer",
        "range": [120, 120],
        "dev": "ff:ff",  # the kernel's device field, as read (not trusted for matching)
        "waiting": False,
    }
    mine = next(h for h in result["holders"] if h["pid"] == os.getpid())
    assert mine["self"] is True and mine["label"] == "self"
    assert result["writer_pids"] == [4242]


def test_writer_candidates_require_owned_posix_write_ranges(tmp_path):
    db = _db_files(tmp_path)
    inode = os.stat(f"{db}-shm").st_ino
    extra = (
        f"6: POSIX ADVISORY READ 9001 ff:ff:{inode} 120 120\n"
        f"7: POSIX ADVISORY WRITE 9002 ff:ff:{inode} 120 122\n"
        f"8: -> POSIX ADVISORY WRITE 9003 ff:ff:{inode} 120 122\n"
        f"9: POSIX ADVISORY WRITE 9004 ff:ff:{inode} 121 122\n"
        f"10: OFDLCK ADVISORY WRITE -1 ff:ff:{inode} 120 120\n"
        f"11: FLOCK ADVISORY WRITE 9005 ff:ff:{inode} 0 EOF\n"
        f"12: POSIX ADVISORY WRITE 9006 ff:ff:{inode} 0 119\n"
        f"13: POSIX ADVISORY WRITE 9007 ff:ff:{inode} 120 EOF\n"
    )
    result = lock_holders.holders_for(db, _locks_for(db, extra), label=lambda pid: "test")
    assert result["writer_pids"] == [4242, 9002, 9007]
    # Ineligible writer candidates remain available as raw diagnostic evidence.
    rows = {h["pid"]: h for h in result["holders"]}
    assert rows[9001]["access"] == "READ"
    assert rows[9001]["type"] == "POSIX"
    assert rows[9002]["range"] == [120, 122]
    assert rows[9003]["waiting"] is True
    assert rows[-1]["range"] == [120, 120]
    assert rows[-1]["type"] == "OFDLCK"
    assert rows[9005]["range"] == [0, None]
    assert rows[9005]["type"] == "FLOCK"
    assert rows[9006]["range"] == [0, 119]
    assert rows[9007]["range"] == [120, None]


def test_retention_uses_the_public_prune_helper(tmp_path, monkeypatch):
    from genesis.observability import pid_breakdown

    calls = []
    monkeypatch.setattr(pid_breakdown, "prune_snapshot_dir", lambda d, now: calls.append(d))
    monkeypatch.setattr(lock_holders, "capture", lambda db_path: {"holders": []})
    target = tmp_path / "snaps" / "s.json"
    target.parent.mkdir()
    lock_holders.capture_and_write(target, tmp_path / "x.db", {})
    assert target.exists()
    assert calls == [target.parent]


def test_importing_the_connection_module_stays_light():
    # genesis.db.connection is imported by per-tool-call hooks; the lock-holder
    # capture must not drag genesis.observability (and aiohttp) in at import.

    code = "import sys, genesis.db.connection; print('aiohttp' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "False"


def test_capture_reports_unreadable_lock_table(tmp_path, monkeypatch):
    db = _db_files(tmp_path)
    monkeypatch.setattr(lock_holders, "_PROC_LOCKS", str(tmp_path / "missing"))
    snap = lock_holders.capture(db)
    assert snap["readable"] is False
    assert "missing" in snap["lock_table_error"]
    assert snap["holders"] == []


@pytest.mark.parametrize("error", ["SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT", None])
def test_unreadable_table_preserves_the_original_sqlite_error(tmp_path, monkeypatch, error):
    monkeypatch.setattr(lock_holders, "_PROC_LOCKS", str(tmp_path / "missing"))
    target = tmp_path / "snapshot.json"
    lock_holders.capture_and_write(target, tmp_path / "scratch.db", {"error": error})
    data = json.loads(target.read_text())
    assert data["error"] == error
    assert data["readable"] is False
    assert "missing" in data["lock_table_error"]


@pytest.fixture
def snapshot_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(lock_holders.DISABLE_ENV, raising=False)
    lock_holders._reset_rate_limit()
    yield tmp_path / "home" / "logs" / "lock_snapshots"
    lock_holders._reset_rate_limit()


def test_write_snapshot_lands_json_in_log_dir(tmp_path, snapshot_home, monkeypatch):
    db = _db_files(tmp_path)
    monkeypatch.setattr(lock_holders, "_PROC_LOCKS", str(tmp_path / "locks"))
    (tmp_path / "locks").write_text(_locks_for(db))
    target = lock_holders.snapshot_path()
    assert target.parent == snapshot_home
    lock_holders.capture_and_write(
        target,
        db,
        {"attempts": 4, "backoff_s": 1.5, "statement": "INSERT INTO t VALUES (?)"},
    )
    data = json.loads(target.read_text())
    assert data["db_path"] == str(db)
    assert data["attempts"] == 4 and data["statement"] == "INSERT INTO t VALUES (?)"
    assert data["writer_pids"] == [4242]
    assert data["readable"] is True


_CHILD = textwrap.dedent(
    """\
    import sqlite3, sys
    c = sqlite3.connect(sys.argv[1], isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS t(x)")
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO t VALUES (1)")
    print("ready", flush=True)
    sys.stdin.read()
    """
)


def test_child_holding_begin_immediate_is_reported_as_writer(tmp_path):
    if not os.path.exists("/proc/locks"):
        pytest.skip("/proc/locks is not available on this platform")
    db = tmp_path / "live.db"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        snap = lock_holders.capture(db)
        if not snap["readable"]:
            pytest.skip(f"/proc/locks unreadable here: {snap['lock_table_error']}")
        if not snap["holders"]:
            pytest.skip("no /proc/locks line matched the database files here")
        assert child.pid in snap["writer_pids"], snap
        writer = next(h for h in snap["holders"] if h["pid"] == child.pid and h["lock"] == "writer")
        assert writer["file"] == "shm" and writer["access"] == "WRITE"
        assert writer["label"]  # a credential-free process label
    finally:
        child.stdin.close()
        child.wait(timeout=30)
