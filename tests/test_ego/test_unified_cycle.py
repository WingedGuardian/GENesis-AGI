"""Tests for the unified cognitive loop — run_unified_cycle and CRUD.

Covers: perceive→think→act→learn pipeline, cycle outcome recording,
budget check, focused prompt building.
"""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import ego as ego_crud
from genesis.ego.signals import EgoSignal

# ── CRUD: ego_cycle_outcomes ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_list_cycle_outcomes(db):
    """create_cycle_outcome should insert, list_cycle_outcomes should retrieve."""
    cid = await ego_crud.create_cycle_outcome(
        db,
        cycle_id="cycle-unified-1",
        focus_type="proactive",
        focus_id=None,
        num_proposals=2,
        signals_consumed=json.dumps(["sig_001", "sig_002"]),
        perception_rationale="Idle tick, no competing signals",
        perceive_cost_usd=0.0,
    )
    assert cid == "cycle-unified-1"

    rows = await ego_crud.list_cycle_outcomes(db, limit=10)
    assert len(rows) >= 1
    row = rows[0]
    assert row["cycle_id"] == "cycle-unified-1"
    assert row["focus_type"] == "proactive"
    assert row["num_proposals"] == 2
    assert row["perceive_cost_usd"] == 0.0
    assert "sig_001" in row["signals_consumed"]


@pytest.mark.asyncio
async def test_list_cycle_outcomes_ordered_newest_first(db):
    """Outcomes should be ordered by created_at descending."""
    await ego_crud.create_cycle_outcome(
        db,
        cycle_id="cycle-old",
        focus_type="reactive",
    )
    await ego_crud.create_cycle_outcome(
        db,
        cycle_id="cycle-new",
        focus_type="goal_review",
    )

    rows = await ego_crud.list_cycle_outcomes(db, limit=10)
    assert len(rows) >= 2
    # Most recent should be first
    assert rows[0]["cycle_id"] == "cycle-new"


@pytest.mark.asyncio
async def test_create_cycle_outcome_with_focus_id(db):
    """focus_id should be stored correctly."""
    await ego_crud.create_cycle_outcome(
        db,
        cycle_id="cycle-goal-1",
        focus_type="goal_review",
        focus_id="goal_abc123",
        perception_rationale="Goal stale for 14 days",
    )
    rows = await ego_crud.list_cycle_outcomes(db, limit=1)
    assert rows[0]["focus_id"] == "goal_abc123"
    assert rows[0]["perception_rationale"] == "Goal stale for 14 days"


@pytest.mark.asyncio
async def test_get_latest_goal_assessment_returns_newest(db):
    """get_latest_goal_assessment returns the most recent non-empty verdict
    for a goal, ignoring NULL/empty rows and other goals."""
    # Older assessment for the goal.
    await ego_crud.create_cycle_outcome(
        db, cycle_id="c-old", focus_type="goal_review",
        focus_id="goal_x", assessment="early: keep going",
    )
    # A NULL-assessment cycle for the same goal (non-goal_review) — ignored.
    await ego_crud.create_cycle_outcome(
        db, cycle_id="c-null", focus_type="proactive", focus_id="goal_x",
    )
    # Newer assessment for the goal — this one wins.
    await ego_crud.create_cycle_outcome(
        db, cycle_id="c-new", focus_type="goal_review",
        focus_id="goal_x", assessment="later: stalled, replan",
    )
    # A different goal — must not leak.
    await ego_crud.create_cycle_outcome(
        db, cycle_id="c-other", focus_type="goal_review",
        focus_id="goal_y", assessment="unrelated",
    )
    got = await ego_crud.get_latest_goal_assessment(db, "goal_x")
    assert got == "later: stalled, replan"


@pytest.mark.asyncio
async def test_get_latest_goal_assessment_none_when_absent(db):
    """No assessment rows for the goal → None (empty-state safe)."""
    await ego_crud.create_cycle_outcome(
        db, cycle_id="c1", focus_type="proactive", focus_id="goal_z",
    )
    assert await ego_crud.get_latest_goal_assessment(db, "goal_z") is None
    assert await ego_crud.get_latest_goal_assessment(db, "missing") is None


# ── EgoSignal integration ─────────────────────────────────────────────────


def test_ego_signal_imports_cleanly():
    """Verify EgoSignal can be imported and instantiated."""
    sig = EgoSignal(
        signal_type="timer",
        focus_category="proactive",
        summary="Idle tick",
        priority="medium",
    )
    assert sig.signal_type == "timer"
    assert sig.focus_category == "proactive"


# ── Focused prompt building ───────────────────────────────────────────────


def test_build_focused_prompt_proactive():
    """Proactive focus should produce a standard directive."""
    from genesis.ego.focus import FocusResult
    from genesis.ego.session import EgoSession

    # We only need the method, not a full session — test via direct call.
    # _build_focused_prompt is a pure function (no I/O, no state).
    focus = FocusResult(
        focus_type="proactive",
        rationale="idle tick, no competing signals",
    )
    # Call the unbound method with a mock self
    prompt = EgoSession._build_focused_prompt(
        None,  # self (not used for this method)
        dynamic_context="## Test Context\nSome data here.",
        focus=focus,
    )
    assert "proactive" in prompt
    assert "idle tick" in prompt
    assert "Test Context" in prompt
    # Should NOT contain morning report instructions
    assert "MORNING REPORT" not in prompt
    assert "DAILY BRIEFING" not in prompt


def test_build_focused_prompt_daily_briefing():
    """Daily briefing focus prioritizes the user's day — no morning_report field."""
    from genesis.ego.focus import FocusResult
    from genesis.ego.session import EgoSession

    focus = FocusResult(
        focus_type="daily_briefing",
        rationale="scheduled morning briefing",
    )
    prompt = EgoSession._build_focused_prompt(
        None,
        dynamic_context="## Context",
        focus=focus,
    )
    assert "DAILY BRIEFING" in prompt
    assert "needs today" in prompt
    # The real morning report pipeline is the sole source — the ego must
    # not be instructed to produce a morning_report field.
    assert "morning_report" not in prompt


def test_build_focused_prompt_goal_review():
    """Goal review focus should include goal assessment instructions."""
    from genesis.ego.focus import FocusResult
    from genesis.ego.session import EgoSession

    focus = FocusResult(
        focus_type="goal_review",
        focus_id="goal_xyz",
        rationale="goal stale for 12 days",
    )
    prompt = EgoSession._build_focused_prompt(
        None,
        dynamic_context="## Context",
        focus=focus,
    )
    assert "GOAL REVIEW" in prompt
    assert "goal_assessment" in prompt
    assert "goal_review" in prompt


def test_build_focused_prompt_reactive():
    """Reactive focus should include reactive instructions."""
    from genesis.ego.focus import FocusResult
    from genesis.ego.session import EgoSession

    focus = FocusResult(
        focus_type="reactive",
        rationale="health alert received",
    )
    prompt = EgoSession._build_focused_prompt(
        None,
        dynamic_context="## Context",
        focus=focus,
    )
    assert "REACTIVE" in prompt
    assert "event(s)" in prompt


# ── FocusCategory enum ────────────────────────────────────────────────────


def test_focus_category_values():
    """Verify FocusCategory enum has expected values."""
    from genesis.ego.types import FocusCategory

    assert FocusCategory.PROACTIVE == "proactive"
    assert FocusCategory.DAILY_BRIEFING == "daily_briefing"
    assert FocusCategory.REACTIVE == "reactive"
    assert FocusCategory.GOAL_REVIEW == "goal_review"
    assert FocusCategory.DISPATCH_OUTCOME == "dispatch_outcome"
    assert FocusCategory.ESCALATION == "escalation"


# ── EgoConfig goal_review_staleness_days ──────────────────────────────────


def test_ego_config_has_staleness_field():
    from genesis.ego.types import EgoConfig

    config = EgoConfig()
    assert config.goal_review_staleness_days == 10

    config2 = EgoConfig(goal_review_staleness_days=7)
    assert config2.goal_review_staleness_days == 7
