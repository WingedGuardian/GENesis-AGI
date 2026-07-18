"""d0002 — resolve alerts stranded by the duplicate-session guard removal.

The guard's awareness-loop resolver is deleted in the same change, so an
install upgrading with an open ``duplicate_session_monitor`` alert has
nothing left that knows the source string — d0002 closes them once.
Column names mirror ``crud/observations.py::resolve_by_source_and_type``
(resolved / resolved_at / resolution_notes), which is what created them.
"""

from __future__ import annotations

import sqlite3

import genesis.db.data_migrations.d0002_resolve_duplicate_session_alerts as d0002


def _seed_db(path) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE observations (id TEXT PRIMARY KEY, source TEXT, "
        "resolved INTEGER DEFAULT 0, resolved_at TEXT, resolution_notes TEXT)"
    )
    db.executemany(
        "INSERT INTO observations (id, source, resolved) VALUES (?, ?, ?)",
        [
            ("open-guard", "duplicate_session_monitor", 0),
            ("closed-guard", "duplicate_session_monitor", 1),
            ("open-other", "deploy_staleness_monitor", 0),
        ],
    )
    db.commit()
    db.close()


def test_resolves_only_open_guard_alerts(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    _seed_db(db_path)
    monkeypatch.setattr(d0002, "genesis_db_path", lambda: str(db_path))

    assert d0002.verify() is False
    assert d0002.migrate() == {"resolved": 1}
    assert d0002.verify() is True

    db = sqlite3.connect(db_path)
    rows = dict(db.execute("SELECT id, resolved FROM observations").fetchall())
    assert rows == {"open-guard": 1, "closed-guard": 1, "open-other": 0}
    notes = db.execute(
        "SELECT resolution_notes FROM observations WHERE id='open-guard'"
    ).fetchone()[0]
    assert "d0002" in notes
    db.close()


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    _seed_db(db_path)
    monkeypatch.setattr(d0002, "genesis_db_path", lambda: str(db_path))

    assert d0002.migrate() == {"resolved": 1}
    assert d0002.migrate() == {"resolved": 0}
    assert d0002.verify() is True


def test_untouched_db_verifies_clean(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    db = sqlite3.connect(db_path)
    db.execute(
        "CREATE TABLE observations (id TEXT PRIMARY KEY, source TEXT, "
        "resolved INTEGER DEFAULT 0, resolved_at TEXT, resolution_notes TEXT)"
    )
    db.commit()
    db.close()
    monkeypatch.setattr(d0002, "genesis_db_path", lambda: str(db_path))
    assert d0002.verify() is True
    assert d0002.migrate() == {"resolved": 0}
