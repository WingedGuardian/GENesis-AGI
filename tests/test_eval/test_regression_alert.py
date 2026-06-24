"""Tests for J-9 subsystem-grade regression detection + human-gated surfacing."""

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import j9_eval
from genesis.db.schema import create_all_tables
from genesis.eval.regression_alert import (
    _proposal_id,
    _regression_reason,
    check_and_alert_regressions,
)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _grade(db, subsystem, grade, score, period_end):
    await j9_eval.insert_subsystem_grade(
        db,
        period_start="2026-06-08T00:00:00Z",
        period_end=period_end,
        period_type="weekly",
        subsystem=subsystem,
        grade=grade,
        score=score,
        factors={"f": 1.0},
        sample_count=10,
    )


# ── _regression_reason (pure threshold logic) ────────────────────────────────

def test_reason_none_grade_never_alerts():
    """Cold-start / insufficient-data weeks (grade None) never alert."""
    assert _regression_reason({"grade": None, "score": None}, None) is None


def test_reason_healthy_grade_no_drop():
    assert _regression_reason(
        {"grade": "B", "score": 82.0}, {"grade": "B", "score": 83.0},
    ) is None


def test_reason_absolute_floor_f():
    r = _regression_reason({"grade": "F", "score": 55.0}, None)
    assert r is not None and "grade F" in r


def test_reason_delta_drop_15plus():
    r = _regression_reason(
        {"grade": "C", "score": 70.0}, {"grade": "B", "score": 88.0},
    )
    assert r is not None and "dropped" in r


def test_reason_small_drop_below_threshold():
    # 10-pt drop is below the 15-pt threshold and grade is not F → no alert.
    assert _regression_reason(
        {"grade": "B", "score": 80.0}, {"grade": "A", "score": 90.0},
    ) is None


# ── check_and_alert_regressions (integration) ────────────────────────────────

@pytest.mark.asyncio
async def test_f_grade_surfaces_alert_and_proposal(db):
    await _grade(db, "memory", "F", 55.0, "2026-06-22T00:00:00Z")
    pipeline = AsyncMock()

    handled = await check_and_alert_regressions(db, pipeline)

    assert len(handled) == 1
    assert handled[0]["subsystem"] == "memory"
    pipeline.submit_raw.assert_awaited_once()

    pid = _proposal_id("memory", "2026-06-22T00:00:00Z")
    prop = await ego_crud.get_proposal(db, pid)
    assert prop is not None
    assert prop["action_type"] == "j9_regression"
    assert prop["status"] == "pending"  # human-gated; nothing auto-applied


@pytest.mark.asyncio
async def test_healthy_grades_no_surfacing(db):
    await _grade(db, "memory", "A", 92.0, "2026-06-22T00:00:00Z")
    await _grade(db, "ego", "B", 81.0, "2026-06-22T00:00:00Z")
    pipeline = AsyncMock()

    handled = await check_and_alert_regressions(db, pipeline)

    assert handled == []
    pipeline.submit_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_start_none_grade_no_surfacing(db):
    await _grade(db, "awareness", None, None, "2026-06-22T00:00:00Z")
    pipeline = AsyncMock()

    handled = await check_and_alert_regressions(db, pipeline)

    assert handled == []
    pipeline.submit_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotent_no_double_file_or_alert(db):
    await _grade(db, "memory", "F", 55.0, "2026-06-22T00:00:00Z")
    pipeline = AsyncMock()

    first = await check_and_alert_regressions(db, pipeline)
    second = await check_and_alert_regressions(db, pipeline)

    assert len(first) == 1
    assert second == []  # the proposal marks the period handled
    pipeline.submit_raw.assert_awaited_once()  # exactly one alert, not two


@pytest.mark.asyncio
async def test_no_pipeline_still_files_proposal(db):
    await _grade(db, "memory", "F", 55.0, "2026-06-22T00:00:00Z")

    handled = await check_and_alert_regressions(db, None)

    assert len(handled) == 1
    pid = _proposal_id("memory", "2026-06-22T00:00:00Z")
    assert await ego_crud.get_proposal(db, pid) is not None


@pytest.mark.asyncio
async def test_week_over_week_drop_surfaces(db):
    # prior week B(85), this week C(68) → 17-pt drop ≥ 15
    await _grade(db, "ego", "B", 85.0, "2026-06-15T00:00:00Z")
    await _grade(db, "ego", "C", 68.0, "2026-06-22T00:00:00Z")
    pipeline = AsyncMock()

    handled = await check_and_alert_regressions(db, pipeline)

    assert len(handled) == 1
    assert "dropped" in handled[0]["reason"]
