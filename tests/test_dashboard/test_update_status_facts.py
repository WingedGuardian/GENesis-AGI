"""update_status() orders last_update by instant and exposes per-row facts.

The stored row's facts are computed BEFORE the failed/rolled_back→success
reconciliation, so a reconciled row reports the facts the row actually earned
(server_restarted=None) rather than inheriting a success claim it never made.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard.routes import updates

_DDL = """
CREATE TABLE observations (
    id TEXT PRIMARY KEY, type TEXT, content TEXT,
    resolved INTEGER, resolved_at TEXT, resolution_notes TEXT, created_at TEXT
);
CREATE TABLE update_history (
    id TEXT PRIMARY KEY,
    old_tag TEXT, new_tag TEXT, old_commit TEXT, new_commit TEXT,
    status TEXT NOT NULL, rollback_tag TEXT, failure_reason TEXT,
    degraded_subsystems TEXT,
    started_at TEXT NOT NULL, completed_at TEXT
)
"""


def _seed(db_path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    for r in rows:
        conn.execute(
            "INSERT INTO update_history "
            "(id, old_tag, new_tag, old_commit, new_commit, status, "
            " rollback_tag, failure_reason, degraded_subsystems, "
            " started_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["id"], "v0.3.0", "v0.3.1", "abc1234",
                r.get("new_commit", "def5678"), r["status"],
                None, r.get("failure_reason"),
                r.get("degraded_subsystems"), r["started_at"],
                r.get("completed_at", "2026-04-10T12:00:30+00:00"),
            ),
        )
    conn.commit()
    conn.close()


def _status(db_path: Path):
    """Drive update_status against a real tmp SQLite (plain _query_db reader)."""
    def plain_query(sql, params=()):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()
        return rows

    app = Flask(__name__)
    with (
        app.test_request_context(),
        patch.object(updates, "_DB_PATH", db_path),
        patch.object(updates, "_git", return_value=None),
        patch.object(updates, "_query_db", side_effect=plain_query),
    ):
        return updates.update_status().get_json()


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "genesis.db"


def test_dst_fallback_picks_the_later_instant(tmp_db):
    """01:10-05:00 (06:10Z) is newer than 01:30-04:00 (05:30Z) — text sort
    gets this backwards; datetime(started_at) must not."""
    _seed(tmp_db, [
        {"id": "earlier", "status": "success",
         "started_at": "2026-11-01T01:30:00-04:00"},
        {"id": "later", "status": "success",
         "started_at": "2026-11-01T01:10:00-05:00"},
    ])
    payload = _status(tmp_db)
    assert payload["last_update"]["started_at"] == "2026-11-01T01:10:00-05:00"


def test_not_restarted_success_reports_server_restarted_false(tmp_db):
    _seed(tmp_db, [
        {"id": "nr", "status": "success",
         "degraded_subsystems": "genesis-server-not-restarted",
         "started_at": "2026-04-10T12:00:00+00:00"},
    ])
    last = _status(tmp_db)["last_update"]
    assert last["status"] == "success"
    assert last["code_applied"] is True
    assert last["activation_applied"] is True
    assert last["server_restarted"] is False


def test_plain_success_makes_no_restart_claim(tmp_db):
    """No marker → no restart evidence (pre-#2625 writers could omit it)."""
    _seed(tmp_db, [
        {"id": "s", "status": "success",
         "started_at": "2026-04-10T12:00:00+00:00"},
    ])
    last = _status(tmp_db)["last_update"]
    assert last["status"] == "success"
    assert last["server_restarted"] is None


def test_reconciled_row_keeps_facts_from_stored_status(tmp_db):
    """A failed row reconciled to success (its commit landed) must not gain a
    server_restarted claim — the stored row was 'failed', so it stays None."""
    _seed(tmp_db, [
        {"id": "f", "status": "failed",
         "new_commit": "def5678",
         "failure_reason": "health check timeout",
         "started_at": "2026-04-10T12:00:00+00:00"},
    ])

    def git(*args, **kwargs):
        # merge-base --is-ancestor def5678 HEAD → exit 0 (stdout "" is truthy path)
        return ""

    def plain_query(sql, params=()):
        conn = sqlite3.connect(str(tmp_db))
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()
        return rows

    app = Flask(__name__)
    with (
        app.test_request_context(),
        patch.object(updates, "_DB_PATH", tmp_db),
        patch.object(updates, "_git", side_effect=git),
        patch.object(updates, "_query_db", side_effect=plain_query),
    ):
        last = updates.update_status().get_json()["last_update"]
    assert last["reconciled"] is True
    assert last["status"] == "success"
    assert last["code_applied"] is False
    assert last["server_restarted"] is None
