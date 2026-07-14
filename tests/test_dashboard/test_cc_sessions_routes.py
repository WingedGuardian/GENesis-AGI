"""Tests for the CC Sessions modal endpoint (/api/genesis/cc-sessions/detail).

Route-level tests use the mocked-runtime pattern (test_ui_data.py); the
join/merge logic is tested against the real in-memory db fixture through the
extracted _collect_detail helper with injected fake slot lists (no /proc).

The charter tables are created with inline DDL matching migration 0058
(session-manager PR-2a, #1053) so this PR stays independently mergeable; once
0058 lands in db/schema/_tables.py the fixture is redundant but harmless.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import genesis.dashboard.routes  # noqa: F401 — registers routes on blueprint
from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.routes.cc_sessions import _collect_detail

_NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)

_CHARTER_DDL = """
    CREATE TABLE IF NOT EXISTS session_charters (
        session_id       TEXT PRIMARY KEY,
        transcript_path  TEXT,
        origin_prompt    TEXT,
        origin_ts        TEXT,
        mission          TEXT,
        pointers         TEXT NOT NULL DEFAULT '[]',
        compaction_count INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        updated_at       TEXT
    )
"""

_LEDGER_DDL = """
    CREATE TABLE IF NOT EXISTS session_ledger (
        id          TEXT PRIMARY KEY,
        session_id  TEXT NOT NULL,
        text        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open','in_progress','done','absorbed','dropped')),
        source_ref  TEXT,
        added_by    TEXT NOT NULL DEFAULT 'foreground'
                    CHECK(added_by IN ('foreground','ambient','pulse')),
        evidence    TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT
    )
"""


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


async def _seed_session(
    db,
    *,
    sid: str,
    session_type: str = "foreground",
    status: str = "active",
    pid: int | None = None,
    cc_session_id: str | None = None,
    last_activity_delta_h: float = 0.5,
):
    started = (_NOW - timedelta(hours=2)).isoformat()
    last = (_NOW - timedelta(hours=last_activity_delta_h)).isoformat()
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, status, pid,"
        " started_at, last_activity_at, cc_session_id)"
        " VALUES (?, ?, 'test-model', ?, ?, ?, ?, ?)",
        (sid, session_type, status, pid, started, last, cc_session_id),
    )
    await db.commit()


async def _seed_charter_tables(db, *, with_rows: bool = False):
    await db.execute(_CHARTER_DDL)
    await db.execute(_LEDGER_DDL)
    if with_rows:
        await db.execute(
            "INSERT INTO session_charters (session_id, origin_prompt, origin_ts,"
            " mission, pointers, compaction_count, created_at)"
            " VALUES ('cc-abc', 'the origin', '2026-06-30T15:21:06.000Z',"
            " 'ship it', '[]', 3, '2026-07-13T00:00:00+00:00')"
        )
        for i, status in enumerate(["open", "in_progress", "done"]):
            await db.execute(
                "INSERT INTO session_ledger (id, session_id, text, status,"
                " added_by, created_at)"
                f" VALUES ('l{i}', 'cc-abc', 'item {i}', ?, 'foreground',"
                " '2026-07-13T00:00:00+00:00')",
                (status,),
            )
    await db.commit()


def _slot(slot: str, pid: int, rss_mb: float = 512.0, status: str = "healthy") -> dict:
    return {"slot": slot, "pid": pid, "rss_mb": rss_mb, "status": status}


# ── Route level ──────────────────────────────────────────────────────────────


def test_detail_503_when_not_bootstrapped(client):
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = False
    mock_rt.db = None
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/cc-sessions/detail")
    assert resp.status_code == 503
    assert resp.get_json() == {"error": "not bootstrapped"}


def test_route_registered_on_dashboard_blueprint(app):
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/genesis/cc-sessions/detail" in rules


# ── Helper level (real db fixture, injected slots) ───────────────────────────


async def test_slot_merge_by_pid(db):
    await _seed_charter_tables(db)
    await _seed_session(db, sid="s1", pid=100)
    result = await _collect_detail(db, [_slot("1", 100), _slot("2", 200)], now=_NOW)
    (row,) = result["sessions"]
    assert row["live"] == {"slot": "1", "pid": 100, "rss_mb": 512.0, "slot_status": "healthy"}
    assert row["flags"] == []
    assert [s["pid"] for s in result["unmatched_slots"]] == [200]
    assert result["stats"]["live_procs"] == 2
    assert result["stats"]["db_active"] == 1
    assert result["stats"]["discrepant"] == 1  # the unmatched slot


async def test_db_active_no_proc_flag(db):
    await _seed_charter_tables(db)
    await _seed_session(db, sid="s1", pid=None)
    await _seed_session(db, sid="s2", pid=333)
    result = await _collect_detail(db, [], now=_NOW)
    assert all(r["flags"] == ["db_active_no_proc"] for r in result["sessions"])
    assert result["stats"]["discrepant"] == 2


async def test_proc_but_db_inactive_flag(db):
    await _seed_charter_tables(db)
    await _seed_session(db, sid="s1", status="completed", pid=100)
    result = await _collect_detail(db, [_slot("1", 100)], now=_NOW)
    (row,) = result["sessions"]
    assert row["flags"] == ["proc_but_db_inactive"]
    assert result["unmatched_slots"] == []  # consumed by the match
    assert result["stats"]["discrepant"] == 1


async def test_charter_and_ledger_join(db):
    await _seed_charter_tables(db, with_rows=True)
    await _seed_session(db, sid="s1", cc_session_id="cc-abc", pid=None, status="completed")
    await _seed_session(db, sid="s2", cc_session_id="cc-none", pid=None, status="completed")
    result = await _collect_detail(db, [], now=_NOW)
    assert result["charters_available"] is True
    by_id = {r["id"]: r for r in result["sessions"]}
    charter = by_id["s1"]["charter"]
    assert charter["mission"] == "ship it"
    assert charter["compaction_count"] == 3
    assert charter["ledger_open"] == 2  # open + in_progress
    assert charter["ledger_total"] == 3
    assert by_id["s2"]["charter"] is None


async def test_missing_charter_tables_tolerated(db):
    """Pre-0058 install: no session_charters/session_ledger tables."""
    await _seed_session(db, sid="s1", cc_session_id="cc-abc")
    result = await _collect_detail(db, [], now=_NOW)
    assert result["charters_available"] is False
    assert result["sessions"][0]["charter"] is None


async def test_window_keeps_old_active_drops_old_inactive(db):
    await _seed_charter_tables(db)
    await _seed_session(db, sid="old-done", status="completed", last_activity_delta_h=72)
    await _seed_session(db, sid="old-active", status="active", last_activity_delta_h=72)
    await _seed_session(db, sid="recent-done", status="completed", last_activity_delta_h=1)
    result = await _collect_detail(db, [], now=_NOW)
    ids = {r["id"] for r in result["sessions"]}
    assert ids == {"old-active", "recent-done"}


async def test_ages_computed_server_side(db):
    await _seed_charter_tables(db)
    await _seed_session(db, sid="s1", last_activity_delta_h=0.5)
    result = await _collect_detail(db, [], now=_NOW)
    (row,) = result["sessions"]
    assert row["age_s"] == pytest.approx(7200, abs=5)
    assert row["idle_s"] == pytest.approx(1800, abs=5)


async def test_empty_state(db):
    await _seed_charter_tables(db)
    result = await _collect_detail(db, [], now=_NOW)
    assert result["sessions"] == []
    assert result["unmatched_slots"] == []
    assert result["stats"] == {
        "db_active": 0,
        "live_procs": 0,
        "discrepant": 0,
        "completed_24h": 0,
        "failed_24h": 0,
    }
