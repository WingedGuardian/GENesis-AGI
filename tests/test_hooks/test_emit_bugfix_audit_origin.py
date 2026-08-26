"""WS-3: emit_bugfix_audit stamps a definite first-party origin_class.

The post-commit audit writer bypasses the crud chokepoint (stdlib-only hook), so
its raw INSERT must stamp origin itself or the row lands NULL → excluded by the
fail-closed read side. source='post_commit_hook' is a Genesis-authored
first-party source; the row must carry origin_class='first_party'.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_SCRIPTS_HOOKS = Path(__file__).resolve().parent.parent.parent / "scripts" / "hooks"
_HOOK_PATH = _SCRIPTS_HOOKS / "emit_bugfix_audit.py"
_spec = importlib.util.spec_from_file_location("emit_bugfix_audit", _HOOK_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["emit_bugfix_audit"] = _mod
_spec.loader.exec_module(_mod)


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "genesis.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE observations (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            type TEXT NOT NULL,
            category TEXT,
            content TEXT NOT NULL,
            priority TEXT NOT NULL,
            created_at TEXT NOT NULL,
            content_hash TEXT,
            expires_at TEXT,
            origin_class TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


def test_bugfix_audit_stamps_first_party(tmp_path: Path, monkeypatch) -> None:
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("GENESIS_DB_PATH", str(db_path))

    rc = _mod.emit("abc123def456", "fix(memory): patch a leak")
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT source, type, origin_class FROM observations").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "post_commit_hook"
    assert row[1] == "bugfix_committed"
    assert row[2] == "first_party"


def test_bugfix_audit_dedup_still_works(tmp_path: Path, monkeypatch) -> None:
    """Origin stamp must not disturb the (source, content_hash) idempotence."""
    db_path = _make_db(tmp_path)
    monkeypatch.setenv("GENESIS_DB_PATH", str(db_path))

    _mod.emit("sha1", "fix(x): same subject")
    _mod.emit("sha1", "fix(x): same subject")  # identical content → dedup

    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    conn.close()
    assert n == 1
