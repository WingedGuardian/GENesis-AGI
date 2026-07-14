"""Tests for the one-off charter.json → session_charters backfill script."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"

_bf_spec = importlib.util.spec_from_file_location(
    "backfill_session_charters", _SCRIPTS_DIR / "backfill_session_charters.py"
)
_bf = importlib.util.module_from_spec(_bf_spec)
_bf_spec.loader.exec_module(_bf)


def _setup(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    from genesis.db.schema._tables import TABLES

    db_path = tmp_path / "genesis.db"
    conn = sqlite3.connect(db_path)
    conn.execute(TABLES["session_charters"])
    conn.execute(TABLES["session_ledger"])
    conn.commit()
    conn.close()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setenv("GENESIS_DB_PATH", str(db_path))
    monkeypatch.setenv("GENESIS_SESSIONS_DIR", str(sessions_dir))
    return db_path, sessions_dir


def _write_legacy(sessions_dir: Path, session_id: str, **overrides) -> Path:
    charter = {
        "session_id": session_id,
        "origin_prompt": f"origin for {session_id}",
        "origin_ts": "2026-06-30T15:21:06.000Z",
        "created_at": "2026-07-13T02:00:00+00:00",
        "compaction_count": 2,
        "mission": None,
        "pointers": ["~/.claude/plans/x.md"],
        **overrides,
    }
    d = sessions_dir / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "charter.json").write_text(json.dumps(charter))
    return d / "charter.json"


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["backfill_session_charters.py", *argv])
    return _bf.main()


def _rows(db_path: Path) -> dict[str, dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = {
        r["session_id"]: dict(r) for r in conn.execute("SELECT * FROM session_charters").fetchall()
    }
    conn.close()
    return rows


def test_import_and_idempotent_rerun(monkeypatch, tmp_path, capsys):
    db_path, sessions_dir = _setup(tmp_path, monkeypatch)
    _write_legacy(sessions_dir, "sid-a")
    _write_legacy(sessions_dir, "sid-b", mission="keep me")

    assert _run(monkeypatch) == 0
    rows = _rows(db_path)
    assert rows["sid-a"]["origin_prompt"] == "origin for sid-a"
    assert rows["sid-b"]["mission"] == "keep me"
    assert rows["sid-a"]["compaction_count"] == 2
    assert json.loads(rows["sid-a"]["pointers"]) == ["~/.claude/plans/x.md"]
    # mirror regenerated
    assert (sessions_dir / "sid-a" / "charter.md").exists()
    # legacy file left in place (rollback fallback)
    assert (sessions_dir / "sid-a" / "charter.json").exists()

    # Re-run: imports nothing, changes nothing
    assert _run(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "imported 0, origin-filled 0, already-in-db 2" in out


def test_rerun_preserves_db_edits(monkeypatch, tmp_path):
    """A row the MCP tools already edited must survive a backfill re-run."""
    db_path, sessions_dir = _setup(tmp_path, monkeypatch)
    _write_legacy(sessions_dir, "sid-a")
    assert _run(monkeypatch) == 0

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE session_charters SET mission = 'edited via MCP' WHERE session_id = 'sid-a'"
    )
    conn.commit()
    conn.close()

    assert _run(monkeypatch) == 0
    assert _rows(db_path)["sid-a"]["mission"] == "edited via MCP"


def test_stub_row_gets_origin_filled(monkeypatch, tmp_path, capsys):
    """Legacy charter.json + a pre-backfill MCP stub row: the backfill must
    fill the missing origin (Codex P2, PR #1053), preserving living fields."""
    db_path, sessions_dir = _setup(tmp_path, monkeypatch)
    _write_legacy(sessions_dir, "sid-stub")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO session_charters (session_id, mission, pointers,"
        " compaction_count, created_at)"
        " VALUES ('sid-stub', 'mcp mission', '[]', 0, '2026-07-14T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    assert _run(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "imported 0, origin-filled 1, already-in-db 0" in out
    row = _rows(db_path)["sid-stub"]
    assert row["origin_prompt"] == "origin for sid-stub"
    assert row["mission"] == "mcp mission"


def test_invalid_json_skipped_and_counted(monkeypatch, tmp_path, capsys):
    db_path, sessions_dir = _setup(tmp_path, monkeypatch)
    _write_legacy(sessions_dir, "sid-good")
    bad = sessions_dir / "sid-bad"
    bad.mkdir()
    (bad / "charter.json").write_text("{corrupt")
    empty = sessions_dir / "sid-empty"
    empty.mkdir()
    (empty / "charter.json").write_text(json.dumps({"session_id": "sid-empty"}))

    assert _run(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "imported 1, origin-filled 0, already-in-db 0, invalid 2" in out
    rows = _rows(db_path)
    assert "sid-good" in rows
    assert "sid-bad" not in rows


def test_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    db_path, sessions_dir = _setup(tmp_path, monkeypatch)
    _write_legacy(sessions_dir, "sid-a")
    assert _run(monkeypatch, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would import sid-a" in out
    assert _rows(db_path) == {}
    assert not (sessions_dir / "sid-a" / "charter.md").exists()


def test_missing_table_errors(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "genesis.db"
    sqlite3.connect(db_path).close()
    (tmp_path / "sessions").mkdir()
    monkeypatch.setenv("GENESIS_DB_PATH", str(db_path))
    monkeypatch.setenv("GENESIS_SESSIONS_DIR", str(tmp_path / "sessions"))
    assert _run(monkeypatch) == 1
    assert "run migrations first" in capsys.readouterr().err
