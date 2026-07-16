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
                f" '2026-07-13T00:00:0{i}+00:00')",
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
    """Pre-0058 install: no session_charters/session_ledger tables.

    Migration 0058 is on main now, so the shared fixture creates the tables —
    drop them explicitly to simulate an un-migrated install.
    """
    await db.execute("DROP TABLE IF EXISTS session_charters")
    await db.execute("DROP TABLE IF EXISTS session_ledger")
    await db.commit()
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


# ── Per-session cockpit endpoint (session-manager PR-4b) ─────────────────────


def test_charter_route_registered(app):
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/genesis/cc-sessions/<cc_session_id>/charter" in rules


def test_charter_route_503_when_not_bootstrapped(client):
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = False
    mock_rt.db = None
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/cc-sessions/cc-abc/charter")
    assert resp.status_code == 503


def test_charter_route_traversal_id_rejected(client):
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/cc-sessions/../charter")
    assert resp.status_code in (400, 404)  # our guard or Flask routing — never a read
    assert mock_rt.db.execute.call_count == 0


async def _seed_cockpit(db, tmp_path, *, waypoint_lines=None):
    """cc_sessions row + charter + ledger + waypoints + pulse annotation."""
    from genesis.dashboard.routes.cc_sessions import _collect_session_detail

    await _seed_charter_tables(db, with_rows=True)
    await _seed_session(db, sid="cc-abc", cc_session_id="cc-abc")
    sessions_dir = tmp_path / "sessions"
    (sessions_dir / "cc-abc").mkdir(parents=True)
    lines = (
        waypoint_lines
        if waypoint_lines is not None
        else [
            '{"ts": "2026-07-13T00:00:00+00:00", "trigger": "auto", "transcript_bytes": 100}',
            "{corrupt json",
            '{"ts": "2026-07-14T00:00:00+00:00", "trigger": "manual", "transcript_bytes": 200}',
        ]
    )
    (sessions_dir / "cc-abc" / "waypoints.jsonl").write_text("\n".join(lines) + "\n")
    await db.execute(
        "INSERT INTO repo_pulse_runs (run_id, started_at, trigger, status)"
        " VALUES ('r1', '2026-07-14T00:00:00+00:00', 'manual', 'ok')"
    )
    await db.execute(
        "INSERT INTO repo_pulse_annotations (id, run_id, observed_at, tier,"
        " item_id, item_session_id, item_text, pr_number, status, confidence)"
        " VALUES ('a1', 'r1', '2026-07-14T00:00:00+00:00', 'fuzzy', 'l0',"
        " 'cc-abc', 'item 0', 1081, 'proposed', 0.9)"
    )
    await db.commit()
    return _collect_session_detail, sessions_dir


async def test_cockpit_full_payload(db, tmp_path):
    collect, sessions_dir = await _seed_cockpit(db, tmp_path)
    detail = await collect(db, "cc-abc", sessions_dir=sessions_dir, now=_NOW)
    assert detail["session"]["cc_session_id"] == "cc-abc"
    assert detail["session"]["session_row_count"] == 1
    assert detail["charters_available"] is True
    assert detail["charter"]["origin_prompt"] == "the origin"
    assert detail["charter"]["origin_truncated"] is False
    assert detail["charter"]["mission"] == "ship it"
    assert detail["charter"]["pointers"] == []
    items = detail["ledger"]["items"]
    assert [i["id"] for i in items] == ["l0", "l1", "l2"]
    assert {"added_by", "source_ref", "evidence"} <= set(items[0])
    assert detail["ledger"]["counts"] == {"open": 1, "in_progress": 1, "done": 1}
    # waypoints: corrupt line skipped, others parsed in order
    wp = detail["waypoints"]
    assert wp["available"] is True and wp["truncated"] is False
    assert [w["trigger"] for w in wp["items"]] == ["auto", "manual"]
    # pulse panel live
    pulse = detail["pulse"]
    assert pulse["available"] is True
    assert pulse["annotations"][0]["pr_number"] == 1081
    assert pulse["health"]["runs"] == {"ok": 1}


async def test_cockpit_unknown_session_is_none(db, tmp_path):
    from genesis.dashboard.routes.cc_sessions import _collect_session_detail

    await _seed_charter_tables(db)
    assert (
        await _collect_session_detail(db, "nope", sessions_dir=tmp_path / "sessions", now=_NOW)
        is None
    )


async def test_cockpit_charter_only_session(db, tmp_path):
    """A chartered session with no cc_sessions row (pre-registration) still
    renders — session block None, charter present."""
    from genesis.dashboard.routes.cc_sessions import _collect_session_detail

    await _seed_charter_tables(db, with_rows=True)
    detail = await _collect_session_detail(
        db, "cc-abc", sessions_dir=tmp_path / "sessions", now=_NOW
    )
    assert detail["session"] is None
    assert detail["charter"]["origin_prompt"] == "the origin"
    assert detail["waypoints"]["available"] is False


async def test_cockpit_origin_cap_and_truncated_flag(db, tmp_path):
    from genesis.dashboard.routes.cc_sessions import (
        ORIGIN_PROMPT_CAP,
        _collect_session_detail,
    )

    await _seed_charter_tables(db)
    await db.execute(
        "INSERT INTO session_charters (session_id, origin_prompt, pointers, created_at)"
        " VALUES ('cc-big', ?, '[\"a\"]', '2026-07-13T00:00:00+00:00')",
        ("x" * (ORIGIN_PROMPT_CAP + 500),),
    )
    await db.commit()
    detail = await _collect_session_detail(
        db, "cc-big", sessions_dir=tmp_path / "sessions", now=_NOW
    )
    assert len(detail["charter"]["origin_prompt"]) == ORIGIN_PROMPT_CAP
    assert detail["charter"]["origin_truncated"] is True
    assert detail["charter"]["pointers"] == ["a"]


async def test_cockpit_waypoint_tail_truncation(db, tmp_path):
    from genesis.dashboard.routes.cc_sessions import WAYPOINT_TAIL

    lines = [
        f'{{"ts": "2026-07-13T00:00:{i % 60:02d}+00:00", "trigger": "auto", "transcript_bytes": {i}}}'
        for i in range(WAYPOINT_TAIL + 5)
    ]
    collect, sessions_dir = await _seed_cockpit(db, tmp_path, waypoint_lines=lines)
    detail = await collect(db, "cc-abc", sessions_dir=sessions_dir, now=_NOW)
    wp = detail["waypoints"]
    assert wp["truncated"] is True
    assert len(wp["items"]) == WAYPOINT_TAIL
    assert wp["items"][-1]["transcript_bytes"] == WAYPOINT_TAIL + 4  # newest kept


async def test_cockpit_pulse_degrades_without_tables(db, tmp_path):
    """Pre-0062 installs: the pulse panel degrades to available=False —
    the independent-mergeability regression contract, kept forever."""
    from genesis.dashboard.routes.cc_sessions import _collect_session_detail
    from genesis.db.crud import repo_pulse as pulse_crud

    await _seed_charter_tables(db, with_rows=True)
    await db.execute("DROP TABLE repo_pulse_annotations")
    await db.execute("DROP TABLE repo_pulse_runs")
    await db.commit()
    pulse_crud._tables_verified = False
    detail = await _collect_session_detail(
        db, "cc-abc", sessions_dir=tmp_path / "sessions", now=_NOW
    )
    pulse_crud._tables_verified = False
    assert detail["pulse"] == {"available": False, "annotations": [], "health": None}
    assert detail["charter"]["origin_prompt"] == "the origin"  # rest unaffected


# ── Pulse confirm/reject endpoint (PR-4b commit 5) ───────────────────────────


def _mock_rt(db):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = db
    return rt


async def _seed_pulse_proposal(db, *, ann_id="a1", item_id="l0", status="proposed"):
    await db.execute(
        "INSERT INTO repo_pulse_runs (run_id, started_at, trigger, status)"
        " VALUES ('r1', '2026-07-14T00:00:00+00:00', 'manual', 'ok')"
    )
    await db.execute(
        "INSERT INTO repo_pulse_annotations (id, run_id, observed_at, tier,"
        " item_id, item_session_id, item_text, pr_number, pr_title, status, confidence)"
        " VALUES (?, 'r1', '2026-07-14T00:00:00+00:00', 'fuzzy', ?,"
        " 'cc-abc', 'item 0', 1081, 'feat: thing', ?, 0.9)",
        (ann_id, item_id, status),
    )
    await db.commit()


async def _ann_status(db, ann_id="a1"):
    cur = await db.execute("SELECT status FROM repo_pulse_annotations WHERE id = ?", (ann_id,))
    return (await cur.fetchone())[0]


async def _ledger_row(db, item_id="l0"):
    cur = await db.execute("SELECT * FROM session_ledger WHERE id = ?", (item_id,))
    return dict(await cur.fetchone())


def test_pulse_resolve_route_registered(app):
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/genesis/cc-sessions/pulse/<annotation_id>/resolve" in rules


def test_pulse_resolve_503_and_400(client, db):
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = False
    mock_rt.db = None
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        assert client.post(
            "/api/genesis/cc-sessions/pulse/a1/resolve", json={"status": "confirmed"}
        ).status_code == 503
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _mock_rt(db)
        assert client.post(
            "/api/genesis/cc-sessions/pulse/a1/resolve", json={"status": "approved"}
        ).status_code == 400
        assert client.post(
            "/api/genesis/cc-sessions/pulse/a1/resolve", json={}
        ).status_code == 400


async def test_pulse_confirm_absorbs_item_with_pr_evidence(db):
    """Dashboard confirm == the injected in-session hint: absorb the item
    with PR evidence, then mark the annotation confirmed — both resolution
    paths teach the precision metric the same lesson."""
    from genesis.dashboard.routes.cc_sessions import _resolve_pulse

    await _seed_charter_tables(db, with_rows=True)
    await _seed_pulse_proposal(db)
    payload, code = await _resolve_pulse(db, "a1", "confirmed")
    assert code == 200
    assert payload["ok"] is True and payload["item_absorbed"] is True
    assert await _ann_status(db) == "confirmed"
    row = await _ledger_row(db)
    assert row["status"] == "absorbed"
    assert "PR #1081" in row["evidence"]


async def test_pulse_reject_leaves_ledger_untouched(db):
    from genesis.dashboard.routes.cc_sessions import _resolve_pulse

    await _seed_charter_tables(db, with_rows=True)
    await _seed_pulse_proposal(db)
    payload, code = await _resolve_pulse(db, "a1", "rejected")
    assert code == 200
    assert await _ann_status(db) == "rejected"
    row = await _ledger_row(db)
    assert row["status"] == "open"
    assert row["evidence"] is None


async def test_pulse_resolve_404_unknown_and_terminal(db):
    from genesis.dashboard.routes.cc_sessions import _resolve_pulse

    await _seed_charter_tables(db, with_rows=True)
    await _seed_pulse_proposal(db, ann_id="a-done", status="applied")
    assert (await _resolve_pulse(db, "nope", "confirmed"))[1] == 404
    assert (await _resolve_pulse(db, "a-done", "confirmed"))[1] == 404
    assert await _ann_status(db, "a-done") == "applied"  # terminal rows never flip


async def test_pulse_confirm_on_closed_item_still_confirms_annotation(db):
    """Item already done/absorbed in-session: confirm records the metric
    without re-touching the ledger."""
    from genesis.dashboard.routes.cc_sessions import _resolve_pulse

    await _seed_charter_tables(db, with_rows=True)
    await db.execute("UPDATE session_ledger SET status = 'done' WHERE id = 'l0'")
    await db.commit()
    await _seed_pulse_proposal(db)
    payload, code = await _resolve_pulse(db, "a1", "confirmed")
    assert code == 200
    assert payload["item_absorbed"] is False
    assert await _ann_status(db) == "confirmed"
    assert (await _ledger_row(db))["status"] == "done"


async def test_cockpit_falls_back_to_db_row_id(db, tmp_path):
    """Rows registered before their transcript id is known (NULL
    cc_session_id) are addressable by DB row id — the tab's fallback click
    path must open a cockpit, not a 404 (Codex P2 on #1089)."""
    from genesis.dashboard.routes.cc_sessions import _collect_session_detail

    await _seed_charter_tables(db)
    await _seed_session(db, sid="row-only", cc_session_id=None)
    detail = await _collect_session_detail(
        db, "row-only", sessions_dir=tmp_path / "sessions", now=_NOW
    )
    assert detail is not None
    assert detail["session"]["id"] == "row-only"
    assert detail["charter"] is None  # no charter under a row id — fine


async def test_pulse_confirm_resolves_annotation_before_ledger_write(db, monkeypatch):
    """Ordering lock (Codex P2 on #1089): the conditional proposed→confirmed
    flip must WIN before the ledger absorb fires, so a concurrent reject can
    never leave an absorbed item under a rejected annotation."""
    from genesis.dashboard.routes.cc_sessions import _resolve_pulse
    from genesis.db.crud import session_charters as sc

    await _seed_charter_tables(db, with_rows=True)
    await _seed_pulse_proposal(db)
    order: list[str] = []
    real_ledger_update = sc.ledger_update

    async def probing_ledger_update(db_, item_id, **kw):
        order.append(await _ann_status(db_, "a1"))
        return await real_ledger_update(db_, item_id, **kw)

    monkeypatch.setattr(sc, "ledger_update", probing_ledger_update)
    payload, code = await _resolve_pulse(db, "a1", "confirmed")
    assert code == 200 and payload["item_absorbed"] is True
    assert order == ["confirmed"]  # annotation resolved BEFORE the ledger mutate
