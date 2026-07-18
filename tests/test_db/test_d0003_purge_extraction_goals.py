"""d0003 — purge extraction-derived garbage rows from ``user_goals``.

The disabled keyword goal matcher was the ONLY writer of
``evidence_source LIKE 'extraction:%'`` goals (all stamped ``origin='user'``).
Explicit creation (``ego_goal_create`` / foreground) leaves
``evidence_source`` NULL, and ego-owned goals are ``origin='genesis_ego'`` —
both must survive the purge.
"""

from __future__ import annotations

import sqlite3

import genesis.db.data_migrations.d0003_purge_extraction_goals as d0003


def _seed_db(path) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE user_goals (id TEXT PRIMARY KEY, title TEXT, "
        "origin TEXT, evidence_source TEXT)"
    )
    db.executemany(
        "INSERT INTO user_goals (id, title, origin, evidence_source) VALUES (?, ?, ?, ?)",
        [
            # garbage — matcher-written, must be purged
            ("g1", "Migration 0020 applied to add goal_id column", "user", "extraction:f06ace55"),
            (
                "g2",
                "Genesis has a 60% overall confidence in the plan",
                "user",
                "extraction:ed0eb3c8",
            ),
            # legit user goal via explicit creation — evidence_source NULL, keep
            ("keep-user", "Land a data-analyst role", "user", None),
            # ego-owned goal — origin genesis_ego, keep even if evidence looks
            # extraction-ish (matcher never wrote this lane)
            ("keep-ego", "Improve recall precision", "genesis_ego", "extraction:should-not-match"),
        ],
    )
    db.commit()
    db.close()


def test_purges_only_extraction_user_goals(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    _seed_db(db_path)
    monkeypatch.setattr(d0003, "genesis_db_path", lambda: str(db_path))

    assert d0003.verify() is False
    assert d0003.migrate() == {"purged": 2}
    assert d0003.verify() is True

    db = sqlite3.connect(db_path)
    remaining = {r[0] for r in db.execute("SELECT id FROM user_goals").fetchall()}
    db.close()
    # Both garbage rows gone; the legit user goal and the ego goal survive.
    assert remaining == {"keep-user", "keep-ego"}


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    _seed_db(db_path)
    monkeypatch.setattr(d0003, "genesis_db_path", lambda: str(db_path))

    assert d0003.migrate() == {"purged": 2}
    assert d0003.migrate() == {"purged": 0}
    assert d0003.verify() is True


def test_untouched_db_verifies_clean(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    db = sqlite3.connect(db_path)
    db.execute(
        "CREATE TABLE user_goals (id TEXT PRIMARY KEY, title TEXT, "
        "origin TEXT, evidence_source TEXT)"
    )
    db.commit()
    db.close()
    monkeypatch.setattr(d0003, "genesis_db_path", lambda: str(db_path))
    assert d0003.verify() is True
    assert d0003.migrate() == {"purged": 0}
