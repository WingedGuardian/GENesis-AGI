"""Tests for goal_status_change (Phase 6): the shared resolution helper that
applies an approved pause/deprioritize, plus a guard that the helper is wired
into every proposal-resolution entry point."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

import genesis
from genesis.db.crud import ego as ego_crud
from genesis.db.crud import user_goals
from genesis.db.schema import create_all_tables
from genesis.ego.goal_actions import (
    _parse_expected_outputs,
    handle_goal_status_change_resolution,
)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _make_goal(db, *, gid="g1", status="active", priority="high"):
    await db.execute(
        "INSERT INTO user_goals "
        "(id, title, category, status, priority, created_at, updated_at) "
        "VALUES (?, 'T', 'project', ?, ?, '2026-06-01', '2026-06-01')",
        (gid, status, priority),
    )
    await db.commit()


async def _make_gsc_proposal(
    db, *, goal_id, change, value, status="approved", action_type="goal_status_change",
):
    pid = f"gsc_{goal_id}_{change}"
    await ego_crud.create_proposal(
        db,
        id=pid,
        action_type=action_type,
        action_category="goal_management",
        content=f"{change}->{value}",
        status="pending",
        created_at="2026-06-20T00:00:00+00:00",
        goal_id=goal_id,
        expected_outputs=json.dumps({"change": change, "value": value}),
    )
    await ego_crud.resolve_proposal(db, pid, status=status)
    return await ego_crud.get_proposal(db, pid)


# ── _parse_expected_outputs ───────────────────────────────────────────────


def test_parse_expected_outputs():
    assert _parse_expected_outputs('{"change": "status", "value": "paused"}') == {
        "change": "status", "value": "paused",
    }
    assert _parse_expected_outputs({"change": "priority", "value": "low"}) == {
        "change": "priority", "value": "low",
    }
    assert _parse_expected_outputs("not json") is None
    assert _parse_expected_outputs(None) is None
    assert _parse_expected_outputs("[1, 2]") is None  # JSON, but not a dict


# ── handle_goal_status_change_resolution ──────────────────────────────────


async def test_approved_pause_sets_status_and_executes(db):
    await _make_goal(db, gid="g1", status="active")
    prop = await _make_gsc_proposal(db, goal_id="g1", change="status", value="paused")

    applied = await handle_goal_status_change_resolution(db, prop, "approved")

    assert applied is True
    goal = await user_goals.get_by_id(db, "g1")
    assert goal["status"] == "paused"
    row = await ego_crud.get_proposal(db, prop["id"])
    assert row["status"] == "executed"


async def test_approved_deprioritize_sets_priority(db):
    await _make_goal(db, gid="g2", priority="high")
    prop = await _make_gsc_proposal(db, goal_id="g2", change="priority", value="medium")

    applied = await handle_goal_status_change_resolution(db, prop, "approved")

    assert applied is True
    goal = await user_goals.get_by_id(db, "g2")
    assert goal["priority"] == "medium"


async def test_rejected_makes_no_change(db):
    await _make_goal(db, gid="g3", status="active")
    prop = await _make_gsc_proposal(
        db, goal_id="g3", change="status", value="paused", status="rejected",
    )

    applied = await handle_goal_status_change_resolution(db, prop, "rejected")

    assert applied is False
    goal = await user_goals.get_by_id(db, "g3")
    assert goal["status"] == "active"  # untouched — recommend-only


async def test_non_goal_proposal_is_noop(db):
    await _make_goal(db, gid="g4", status="active")
    prop = await _make_gsc_proposal(
        db, goal_id="g4", change="status", value="paused",
        action_type="autonomy_earnback",
    )

    applied = await handle_goal_status_change_resolution(db, prop, "approved")

    assert applied is False
    goal = await user_goals.get_by_id(db, "g4")
    assert goal["status"] == "active"


async def test_invalid_value_is_rejected(db):
    """A terminal/unsupported status must never be applied via this path."""
    await _make_goal(db, gid="g5", status="active")
    prop = await _make_gsc_proposal(db, goal_id="g5", change="status", value="achieved")

    applied = await handle_goal_status_change_resolution(db, prop, "approved")

    assert applied is False
    goal = await user_goals.get_by_id(db, "g5")
    assert goal["status"] == "active"


async def test_garbled_expected_outputs_is_noop(db):
    await _make_goal(db, gid="g6", status="active")
    await ego_crud.create_proposal(
        db, id="g6p", action_type="goal_status_change", content="x",
        status="pending", created_at="2026-06-20T00:00:00+00:00", goal_id="g6",
        expected_outputs="not-json",
    )
    await ego_crud.resolve_proposal(db, "g6p", status="approved")
    prop = await ego_crud.get_proposal(db, "g6p")

    applied = await handle_goal_status_change_resolution(db, prop, "approved")

    assert applied is False
    goal = await user_goals.get_by_id(db, "g6")
    assert goal["status"] == "active"


async def test_missing_goal_still_marks_executed(db):
    """A proposal for a vanished goal applies nothing but is marked executed
    so it doesn't linger as 'approved' and get swept for dispatch."""
    prop = await _make_gsc_proposal(
        db, goal_id="ghost", change="status", value="paused",
    )

    applied = await handle_goal_status_change_resolution(db, prop, "approved")

    assert applied is False
    row = await ego_crud.get_proposal(db, prop["id"])
    assert row["status"] == "executed"


# ── Four-entry-point wiring guard ─────────────────────────────────────────


def test_handler_wired_into_all_resolution_paths():
    """Every proposal-resolution entry point MUST call the goal-status hook,
    or an approval silently no-ops. Reads source from disk (no import side
    effects) so it works for the dashboard routes too."""
    root = Path(genesis.__file__).parent
    entry_points = [
        root / "ego" / "proposals.py",
        root / "mcp" / "health" / "ego_tools.py",
        root / "dashboard" / "routes" / "ego.py",
        root / "dashboard" / "routes" / "comms.py",
    ]
    for path in entry_points:
        src = path.read_text()
        assert "handle_proposal_resolution" in src, (
            f"{path} is missing the shared resolution hook — "
            "a resolution there would silently skip side effects"
        )
    shared = (root / "ego" / "resolution.py").read_text()
    assert "handle_goal_status_change_resolution" in shared, (
        "ego/resolution.py no longer runs handle_goal_status_change_resolution — "
        "that action would silently no-op on every entry point"
    )
