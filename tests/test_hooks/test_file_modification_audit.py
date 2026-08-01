"""Tests for scripts/file_modification_audit_hook.py (PostToolUse Write|Edit).

The hook records an audit row per file edit. These lock in the two hardening
changes:
- ``GENESIS_DB_PATH`` is honored, so a test/verification run writes to an
  override DB and never touches the production ``genesis.db``.
- a file larger than the hash cap is NOT slurped whole for the sha256 — the row
  is still written, with a NULL ``file_hash`` (the column is nullable and has no
  readers; the table is the dead CC-tool audit trail).

The hook is run as a real subprocess with fixture stdin (the shape CC delivers).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
HOOK = REPO_DIR / "scripts" / "file_modification_audit_hook.py"

# Faithful to the live schema (file_path/action/timestamp NOT NULL, file_hash nullable).
_SCHEMA = """
CREATE TABLE file_modifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    file_path  TEXT NOT NULL,
    action     TEXT NOT NULL,
    tool_name  TEXT,
    file_hash  TEXT,
    timestamp  TEXT NOT NULL
);
"""


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _run(db: Path, target: Path, tool: str = "Write") -> subprocess.CompletedProcess:
    payload = {
        "session_id": "test-session",
        "tool_name": tool,
        "tool_input": {"file_path": str(target)},
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "GENESIS_DB_PATH": str(db)},
        timeout=30,
    )


def _rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT file_path, action, tool_name, file_hash FROM file_modifications"
        ).fetchall()
    finally:
        conn.close()


def test_override_db_is_used_not_prod(tmp_path: Path):
    """GENESIS_DB_PATH routes the audit row to the override DB — proving a
    verification run never writes to production genesis.db."""
    db = tmp_path / "override.db"
    _make_db(db)
    target = tmp_path / "edited.py"
    target.write_text("x = 1\n")

    proc = _run(db, target)
    assert proc.returncode == 0

    rows = _rows(db)
    assert len(rows) == 1
    file_path, action, tool_name, file_hash = rows[0]
    assert file_path == str(target)
    assert action == "write" and tool_name == "Write"
    assert file_hash is not None  # small file → hash recorded


def test_edit_action_recorded(tmp_path: Path):
    db = tmp_path / "override.db"
    _make_db(db)
    target = tmp_path / "edited.py"
    target.write_text("y = 2\n")
    proc = _run(db, target, tool="Edit")
    assert proc.returncode == 0
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][1] == "edit"


def test_large_file_hash_null_and_no_stall(tmp_path: Path):
    """A file over the 5 MiB cap is not slurped for hashing — the row is still
    written, with a NULL file_hash."""
    db = tmp_path / "override.db"
    _make_db(db)
    big = tmp_path / "big.bin"
    big.write_bytes(b"\0" * (6 * 1024 * 1024))  # 6 MiB > 5 MiB cap

    proc = _run(db, big)
    assert proc.returncode == 0

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0][0] == str(big)
    assert rows[0][3] is None  # over cap → NULL hash, still audited


def test_just_under_cap_still_hashed(tmp_path: Path):
    """A file at/under the cap is still hashed (boundary check)."""
    db = tmp_path / "override.db"
    _make_db(db)
    f = tmp_path / "small.bin"
    f.write_bytes(b"\0" * (1024 * 1024))  # 1 MiB, well under cap
    proc = _run(db, f)
    assert proc.returncode == 0
    assert _rows(db)[0][3] is not None


def test_missing_db_is_silent(tmp_path: Path):
    """No DB at the override path → hook exits 0, writes nothing (fail-open)."""
    db = tmp_path / "nonexistent.db"  # never created
    target = tmp_path / "edited.py"
    target.write_text("z = 3\n")
    proc = _run(db, target)
    assert proc.returncode == 0
    assert not db.exists()


def test_tilde_in_db_path_is_expanded(tmp_path: Path):
    """A ~/... GENESIS_DB_PATH is expanded (like genesis.env), so the row lands in
    the real home-relative DB rather than a literal './~/...' that never exists."""
    home = tmp_path / "home"
    home.mkdir()
    db = home / "override.db"
    _make_db(db)
    target = tmp_path / "edited.py"
    target.write_text("x = 1\n")
    payload = {"session_id": "s", "tool_name": "Write", "tool_input": {"file_path": str(target)}}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "GENESIS_DB_PATH": "~/override.db"},
        timeout=30,
    )
    assert proc.returncode == 0
    assert len(_rows(db)) == 1  # ~ expanded to $HOME → row landed in the real db


def test_non_write_edit_tool_ignored(tmp_path: Path):
    db = tmp_path / "override.db"
    _make_db(db)
    target = tmp_path / "read.py"
    target.write_text("a = 1\n")
    proc = _run(db, target, tool="Read")
    assert proc.returncode == 0
    assert _rows(db) == []
