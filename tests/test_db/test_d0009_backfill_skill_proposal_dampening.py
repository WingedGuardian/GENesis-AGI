"""d0009 — align pre-existing skill_proposal observations to propose-only.

Pre-propose-only proposal rows carry category=NULL (the exact-match dampening
misses them) and a 14d expiry (they'd auto-resolve before review). This migration
backfills category from content.skill_name and extends expires_at to
created_at + 60d, scoped to UNRESOLVED skill_proposal rows only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import genesis.db.data_migrations.d0009_backfill_skill_proposal_dampening as d0009


def _seed(path, rows) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE observations (id TEXT PRIMARY KEY, type TEXT, resolved INTEGER, "
        "category TEXT, content TEXT, created_at TEXT, expires_at TEXT)"
    )
    db.executemany(
        "INSERT INTO observations "
        "(id, type, resolved, category, content, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()
    db.close()


def test_backfills_category_and_extends_expiry(tmp_path, monkeypatch):
    created = "2026-07-25T00:00:00+00:00"
    old_expiry = "2026-08-08T00:00:00+00:00"  # created + 14d
    new_expiry = (datetime.fromisoformat(created) + timedelta(days=60)).isoformat()
    _seed(
        tmp_path / "genesis.db",
        [
            # old-style unresolved proposal: NULL category, 14d expiry
            (
                "p-old",
                "skill_proposal",
                0,
                None,
                json.dumps({"skill_name": "voice-master", "proposed_content": "x"}),
                created,
                old_expiry,
            ),
            # already-stamped proposal — category + expiry untouched
            (
                "p-new",
                "skill_proposal",
                0,
                "aws-fde",
                json.dumps({"skill_name": "aws-fde"}),
                created,
                new_expiry,
            ),
            # resolved proposal — untouched (only unresolved rows are healed)
            (
                "p-done",
                "skill_proposal",
                1,
                None,
                json.dumps({"skill_name": "gone"}),
                created,
                old_expiry,
            ),
            # unrelated observation type — untouched
            ("o-other", "bug_identified", 0, None, "not json", created, old_expiry),
        ],
    )
    monkeypatch.setattr(d0009, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))

    assert d0009.verify() is False
    result = d0009.migrate()
    assert result["category_filled"] == 1
    assert result["expires_at_bumped"] == 1
    assert d0009.verify() is True

    db = sqlite3.connect(tmp_path / "genesis.db")
    rows = {
        r[0]: r for r in db.execute("SELECT id, category, expires_at FROM observations").fetchall()
    }
    db.close()
    # backfilled: category from content, expiry extended to +60d
    assert rows["p-old"][1] == "voice-master"
    assert rows["p-old"][2] == new_expiry
    # already-stamped: untouched
    assert rows["p-new"][1] == "aws-fde"
    assert rows["p-new"][2] == new_expiry
    # resolved + unrelated type: untouched
    assert rows["p-done"][1] is None
    assert rows["o-other"][1] is None
    assert rows["o-other"][2] == old_expiry

    # Idempotent: a second run heals nothing.
    assert d0009.migrate() == {"category_filled": 0, "expires_at_bumped": 0}


def test_noop_on_fresh_install(tmp_path, monkeypatch):
    _seed(tmp_path / "genesis.db", [])
    monkeypatch.setattr(d0009, "genesis_db_path", lambda: str(tmp_path / "genesis.db"))
    assert d0009.verify() is True
    assert d0009.migrate() == {"category_filled": 0, "expires_at_bumped": 0}
    assert d0009.verify() is True
