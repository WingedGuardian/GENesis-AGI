"""Tests for the per-turn [Charter: ... | open: N] tag in genesis_urgent_alerts.

The tag runs on EVERY prompt: the omission matrix (no row / no DB / no table /
locked DB) is the contract that keeps it free when it has nothing to say.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"

_ua_spec = importlib.util.spec_from_file_location(
    "genesis_urgent_alerts", _SCRIPTS_DIR / "genesis_urgent_alerts.py"
)
_ua = importlib.util.module_from_spec(_ua_spec)
_ua_spec.loader.exec_module(_ua)

SID = "sid-tag-1"


def _make_db(tmp_path: Path) -> Path:
    from genesis.db.schema._tables import TABLES

    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True, exist_ok=True)
    db_file = root / "data" / "genesis.db"
    conn = sqlite3.connect(db_file)
    conn.execute(TABLES["session_charters"])
    conn.execute(TABLES["session_ledger"])
    conn.commit()
    conn.close()
    return root


def _seed(
    tmp_path: Path,
    *,
    mission: str | None = None,
    origin: str | None = "The origin prompt first line.\nSecond line.",
    open_items: int = 0,
    done_items: int = 0,
) -> Path:
    root = _make_db(tmp_path)
    conn = sqlite3.connect(root / "data" / "genesis.db")
    conn.execute(
        "INSERT INTO session_charters (session_id, origin_prompt, mission,"
        " pointers, compaction_count, created_at)"
        " VALUES (?, ?, ?, '[]', 1, '2026-07-13T00:00:00+00:00')",
        (SID, origin, mission),
    )
    for i in range(open_items):
        conn.execute(
            "INSERT INTO session_ledger (id, session_id, text, status, added_by, created_at)"
            " VALUES (?, ?, 'x', ?, 'foreground', '2026-07-13T00:00:00+00:00')",
            (f"o{i}", SID, "open" if i % 2 == 0 else "in_progress"),
        )
    for i in range(done_items):
        conn.execute(
            "INSERT INTO session_ledger (id, session_id, text, status, added_by, created_at)"
            " VALUES (?, ?, 'y', 'done', 'foreground', '2026-07-13T00:00:00+00:00')",
            (f"d{i}", SID),
        )
    conn.commit()
    conn.close()
    return root


def _tag_output(monkeypatch, capsys, root: Path) -> str:
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(root))
    _ua._emit_charter_tag(SID)
    return capsys.readouterr().out


def test_tag_with_mission_and_open_counts(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="Ship the ledger", open_items=3, done_items=2)
    out = _tag_output(monkeypatch, capsys, root)
    assert out.strip() == "[Charter: Ship the ledger | open: 3]"


def test_tag_mission_null_uses_origin_first_line(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, open_items=1)
    out = _tag_output(monkeypatch, capsys, root)
    assert 'origin: "The origin prompt first line."' in out
    assert "Second line" not in out
    assert "| open: 1]" in out


def test_tag_open_zero_still_shown(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m", done_items=4)
    out = _tag_output(monkeypatch, capsys, root)
    assert "| open: 0]" in out


def test_tag_long_mission_truncated(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m" * 200)
    out = _tag_output(monkeypatch, capsys, root)
    assert "m" * 80 + "…" in out
    assert "m" * 81 not in out


def test_tag_omitted_no_row(monkeypatch, capsys, tmp_path):
    root = _make_db(tmp_path)
    assert _tag_output(monkeypatch, capsys, root) == ""


def test_tag_omitted_stub_row_without_origin_or_mission(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, origin=None)
    assert _tag_output(monkeypatch, capsys, root) == ""


def test_tag_omitted_missing_db(monkeypatch, capsys, tmp_path):
    assert _tag_output(monkeypatch, capsys, tmp_path / "nowhere") == ""


def test_tag_omitted_missing_table(monkeypatch, capsys, tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    sqlite3.connect(root / "data" / "genesis.db").close()
    assert _tag_output(monkeypatch, capsys, root) == ""


def test_tag_omitted_locked_db(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m")
    blocker = sqlite3.connect(root / "data" / "genesis.db", timeout=1)
    blocker.isolation_level = None
    blocker.execute("BEGIN EXCLUSIVE")  # blocks even read-only connections
    try:
        assert _tag_output(monkeypatch, capsys, root) == ""
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
