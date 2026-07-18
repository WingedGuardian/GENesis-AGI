"""d0004 — purge job_health rows for jobs whose registration was removed.

Only the code-removed fossil (schedule_infra_monitor, gone in #147) is
purged; a live/dormant-but-registered job keeps its row. Idempotent, and a
no-op on a fresh install (no such row).
"""

from __future__ import annotations

import sqlite3

import genesis.db.data_migrations.d0004_purge_retired_job_health as d0004


def _seed_db(path, *, with_fossil: bool = True) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE job_health (job_name TEXT PRIMARY KEY, last_run TEXT, "
        "last_success TEXT, last_failure TEXT, last_error TEXT, "
        "consecutive_failures INTEGER DEFAULT 0, total_runs INTEGER DEFAULT 0, "
        "total_successes INTEGER DEFAULT 0, total_failures INTEGER DEFAULT 0, "
        "updated_at TEXT)"
    )
    rows = [
        # live/dormant-but-registered jobs — MUST survive
        ("build_lane_poll", "2026-07-07T19:10:03Z"),
        ("infra_profile_refresh", "2026-07-18T10:20:02Z"),
        ("pipeline:prediction-markets", "2026-03-22T21:55:20Z"),
    ]
    if with_fossil:
        rows.append(("schedule_infra_monitor", "2026-04-25T15:42:49Z"))
    db.executemany("INSERT INTO job_health (job_name, last_run) VALUES (?, ?)", rows)
    db.commit()
    db.close()


def test_purges_only_the_retired_fossil(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    _seed_db(db_path)
    monkeypatch.setattr(d0004, "genesis_db_path", lambda: str(db_path))

    assert d0004.verify() is False
    assert d0004.migrate() == {"purged": 1}
    assert d0004.verify() is True

    db = sqlite3.connect(db_path)
    remaining = {r[0] for r in db.execute("SELECT job_name FROM job_health").fetchall()}
    db.close()
    # the fossil is gone; every live/dormant-but-registered row survives
    assert "schedule_infra_monitor" not in remaining
    assert remaining == {
        "build_lane_poll",
        "infra_profile_refresh",
        "pipeline:prediction-markets",
    }


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    _seed_db(db_path)
    monkeypatch.setattr(d0004, "genesis_db_path", lambda: str(db_path))

    assert d0004.migrate() == {"purged": 1}
    assert d0004.migrate() == {"purged": 0}
    assert d0004.verify() is True


def test_fresh_install_no_fossil_is_noop(tmp_path, monkeypatch):
    """Empty-state: an install that never ran the retired job purges nothing."""
    db_path = tmp_path / "genesis.db"
    _seed_db(db_path, with_fossil=False)
    monkeypatch.setattr(d0004, "genesis_db_path", lambda: str(db_path))

    assert d0004.verify() is True  # already clean
    assert d0004.migrate() == {"purged": 0}
