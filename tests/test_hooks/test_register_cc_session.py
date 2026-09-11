"""SessionStart registration hook (register_cc_session).

Acceptance bar (measured 2026-09-04): a live terminal session had NO
cc_sessions row until the 2-hourly adoption poll — and then as
'completed'. The hook writes an honest 'active' row with the claude
ancestor's pid at session start.

Contract under test: NEVER prints (SessionStart stdout is injected into
the session context), exit 0 on every path, never creates the DB file,
headless (-p/--print) invocations skipped.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import aiosqlite
import pytest

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "register_cc_session.py"


def _load():
    spec = importlib.util.spec_from_file_location("register_cc_session", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_proc(tmp_path, chain):
    """Build a /proc-shaped tree: chain = [(pid, comm, ppid, cmdline_argv)]."""
    for pid, comm, ppid, argv in chain:
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


class TestClaudeAncestor:
    def test_finds_interactive_claude(self, tmp_path, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_PROC", str(tmp_path))
        _fake_proc(
            tmp_path,
            [
                (300, "bash", 200, ["bash"]),
                (200, "claude", 100, ["claude", "--dangerously-skip-permissions"]),
                (100, "tmux: server", 1, ["tmux"]),
            ],
        )
        pid, argv = mod._claude_ancestor(300)
        assert pid == 200
        assert "--dangerously-skip-permissions" in argv

    def test_headless_ancestor_detectable(self, tmp_path, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_PROC", str(tmp_path))
        _fake_proc(tmp_path, [(200, "claude", 1, ["claude", "-p", "--model", "x"])])
        pid, argv = mod._claude_ancestor(200)
        assert "-p" in argv

    def test_no_claude_ancestor_is_none(self, tmp_path, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_PROC", str(tmp_path))
        _fake_proc(tmp_path, [(300, "bash", 1, ["bash"])])
        assert mod._claude_ancestor(300) is None

    def test_missing_proc_entry_is_none(self, tmp_path, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "_PROC", str(tmp_path))
        assert mod._claude_ancestor(4242) is None


class TestSilenceContract:
    def _run(self, payload):
        return subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps(payload) if isinstance(payload, dict) else payload,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_no_db_silent_exit_zero(self, monkeypatch):
        monkeypatch.setenv("GENESIS_REPO_ROOT", "/nonexistent-genesis-root")
        # GENESIS_DB_PATH outranks the repo root in genesis_db_path() — an
        # exported value on a dev box would send the subprocess (whose REAL
        # /proc ancestry is an interactive claude when run inside CC) at a
        # live DB (audit finding).
        monkeypatch.delenv("GENESIS_DB_PATH", raising=False)
        r = self._run({"session_id": "11111111-2222-3333-4444-555555555555"})
        assert r.returncode == 0
        assert r.stdout == ""  # byte-identical silence

    def test_malformed_payload_silent_exit_zero(self):
        r = self._run("not-json")
        assert r.returncode == 0
        assert r.stdout == ""

    def test_missing_session_id_silent(self):
        r = self._run({})
        assert r.returncode == 0
        assert r.stdout == ""


@pytest.mark.asyncio
async def test_registration_end_to_end(tmp_path, monkeypatch):
    """Acceptance: session start → honest active row with pid, immediately."""
    from genesis.db.crud import cc_sessions as crud
    from genesis.db.schema import create_all_tables

    db_file = tmp_path / "genesis.db"
    conn = await aiosqlite.connect(db_file)
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.commit()  # release any pending txn — the sync writer needs the lock

    mod = _load()
    monkeypatch.setattr(mod, "_PROC", str(tmp_path / "proc"))
    (tmp_path / "proc").mkdir()
    _fake_proc(
        tmp_path / "proc",
        [
            (300, "python3", 200, ["python3"]),
            (200, "claude", 1, ["claude", "--dangerously-skip-permissions"]),
        ],
    )
    monkeypatch.setattr(mod.os, "getppid", lambda: 300)

    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(mod, "read_payload", lambda: {"session_id": sid})

    import genesis.env as env

    monkeypatch.setattr(env, "genesis_db_path", lambda: db_file)

    mod.main()

    row = await crud.get_by_id(conn, sid)
    assert row is not None
    assert row["status"] == "active"
    assert row["cc_session_id"] == sid
    assert row["pid"] == 200
    await conn.close()


@pytest.mark.asyncio
async def test_resume_reopens_completed_row(tmp_path, monkeypatch):
    """--resume of a dead-adopted session flips it back to active with the
    NEW pid."""
    from genesis.db.crud import cc_sessions as crud
    from genesis.db.crud.cc_sessions import register_terminal_session_sync
    from genesis.db.schema import create_all_tables

    db_file = tmp_path / "genesis.db"
    conn = await aiosqlite.connect(db_file)
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.commit()

    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    await crud.register_from_filesystem(
        conn,
        id=sid,
        cc_session_id=sid,
        started_at="2026-03-07T08:00:00+00:00",
        status="completed",
        completed_at="2026-03-07T08:00:00+00:00",
    )
    register_terminal_session_sync(str(db_file), sid, pid=777, model="opus")
    row = await crud.get_by_id(conn, sid)
    assert row["status"] == "active"
    assert row["completed_at"] is None
    assert row["pid"] == 777
    await conn.close()


def test_pre_migration_table_guard(tmp_path):
    """A DB file with no cc_sessions table → silent no-op, no table created."""
    from genesis.db.crud.cc_sessions import register_terminal_session_sync

    db_file = tmp_path / "genesis.db"
    sqlite3.connect(db_file).close()  # empty db, no tables
    register_terminal_session_sync(str(db_file), "some-id", pid=1)
    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='cc_sessions'").fetchone() is None
    conn.close()
