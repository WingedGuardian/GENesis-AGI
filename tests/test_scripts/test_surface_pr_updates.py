"""E2E: the surface_pr_updates SessionStart hook script.

Drives the real script as a subprocess (the actual entrypoint), with a temp DB
(GENESIS_DB_PATH) and temp home (GENESIS_HOME). Install-agnostic: synthetic
rows, tmp paths, no network, no live services.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "surface_pr_updates.py"
_SRC = Path(__file__).resolve().parents[2] / "src"


def _make_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE outreach_history ("
        "id TEXT PRIMARY KEY, topic TEXT, category TEXT, delivered_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO outreach_history (id, topic, category, delivered_at) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _steward_row(nid: str, days_ago: int = 1) -> tuple:
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return (nid, f"PR steward tick: {nid} was closed", "notification", ts)


def _run(db: Path | None, home: Path, *, disabled=False, cc_session=False) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_SRC)
    env["GENESIS_HOME"] = str(home)
    env.pop("GENESIS_REPO_ROOT", None)  # use worktree config (enabled: true)
    if db is not None:
        env["GENESIS_DB_PATH"] = str(db)
    else:
        env["GENESIS_DB_PATH"] = str(home / "does-not-exist.db")
    if disabled:
        env["GENESIS_PR_WATCH_DISABLED"] = "1"
    else:
        env.pop("GENESIS_PR_WATCH_DISABLED", None)
    if cc_session:
        env["GENESIS_CC_SESSION"] = "1"
    else:
        env.pop("GENESIS_CC_SESSION", None)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout


def test_surfaces_when_match(tmp_path):
    db = tmp_path / "g.db"
    _make_db(db, [_steward_row("litellm#27447")])
    out = _run(db, tmp_path / "home")
    assert "[PRs]" in out
    assert "litellm#27447" in out
    # sidecar was written
    assert (tmp_path / "home" / "pr_watch" / "seen.json").exists()


def test_env_kill_switch_silences(tmp_path):
    db = tmp_path / "g.db"
    _make_db(db, [_steward_row("x")])
    out = _run(db, tmp_path / "home", disabled=True)
    assert out.strip() == ""


def test_dispatched_session_silent_and_leaves_sidecar_untouched(tmp_path):
    db = tmp_path / "g.db"
    _make_db(db, [_steward_row("x")])
    home = tmp_path / "home"
    out = _run(db, home, cc_session=True)
    assert out.strip() == ""
    assert not (home / "pr_watch" / "seen.json").exists()  # never touched


def test_no_matching_rows_silent(tmp_path):
    db = tmp_path / "g.db"
    _make_db(
        db, [("b", "Billy brain is live", "notification", datetime.now(UTC).isoformat())]
    )  # not steward
    out = _run(db, tmp_path / "home")
    assert out.strip() == ""


def test_missing_db_fail_open(tmp_path):
    out = _run(None, tmp_path / "home")  # GENESIS_DB_PATH -> nonexistent
    assert out.strip() == ""


def test_resurfaces_on_second_run(tmp_path):
    db = tmp_path / "g.db"
    _make_db(db, [_steward_row("litellm#27447")])
    home = tmp_path / "home"
    first = _run(db, home)
    second = _run(db, home)  # same day -> within resurface window
    assert "[PRs]" in first
    assert "[PRs]" in second
