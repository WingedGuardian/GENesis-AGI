"""d0007 — clear completed_at orphaned on non-terminal follow_ups rows.

The bug: update_status stamped completed_at on entry to a terminal state but
never cleared it on the way back out, so a wrongly-completed-then-corrected row
kept a lying terminal timestamp (follow-up d67c83c7). This migration heals the
historical rows; the code fix stops new ones.
"""

from __future__ import annotations

import sqlite3

import genesis.db.data_migrations.d0007_clear_orphaned_completed_at as d0007


def _seed(path, rows) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE follow_ups (id TEXT PRIMARY KEY, status TEXT, completed_at TEXT)")
    db.executemany(
        "INSERT INTO follow_ups (id, status, completed_at) VALUES (?, ?, ?)",
        rows,
    )
    db.commit()
    db.close()


def test_clears_orphans_leaves_terminal_and_clean(tmp_path, monkeypatch):
    _seed(
        tmp_path / "genesis.db",
        [
            # orphans: completed_at set on a non-terminal row
            ("orphan-ip", "in_progress", "2026-07-22T14:46:00+00:00"),
            ("orphan-pend", "pending", "2026-07-22T14:46:00+00:00"),
            ("orphan-blocked", "blocked", "2026-07-22T14:46:00+00:00"),
            # legit terminal rows: completed_at is correct, must be untouched
            ("legit-done", "completed", "2026-07-20T00:00:00+00:00"),
            ("legit-fail", "failed", "2026-07-20T00:00:00+00:00"),
            # already-clean non-terminal row
            ("clean-pend", "pending", None),
        ],
    )
    monkeypatch.setattr(d0007, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))

    assert d0007.verify() is False
    assert d0007.migrate() == {"cleared": 3}
    assert d0007.verify() is True

    db = sqlite3.connect(tmp_path / "genesis.db")
    rows = dict(db.execute("SELECT id, completed_at FROM follow_ups").fetchall())
    db.close()
    assert rows["orphan-ip"] is None
    assert rows["orphan-pend"] is None
    assert rows["orphan-blocked"] is None
    assert rows["legit-done"] == "2026-07-20T00:00:00+00:00"  # untouched
    assert rows["legit-fail"] == "2026-07-20T00:00:00+00:00"  # untouched
    assert rows["clean-pend"] is None


def test_noop_on_fresh_install(tmp_path, monkeypatch):
    """No orphans → verify() already True, migrate() a clean no-op."""
    _seed(tmp_path / "genesis.db", [("clean", "pending", None)])
    monkeypatch.setattr(d0007, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))
    assert d0007.verify() is True
    assert d0007.migrate() == {"cleared": 0}
    assert d0007.verify() is True
