"""Tests for CCBudgetTracker."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from genesis.resilience.cc_budget import (
    P_BACKGROUND,
    P_FOREGROUND,
    P_REFLECTION,
    P_SCHEDULED,
    P_SURPLUS,
    P_URGENT,
    CCBudgetTracker,
)
from genesis.resilience.state import CCStatus


@pytest.fixture
async def tracker(db):
    now = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
    return CCBudgetTracker(db, max_sessions_per_hour=20, clock=lambda: now)


async def _insert_sessions(db, count: int, started_at: str):
    for _ in range(count):
        sid = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO cc_sessions
               (id, session_type, model, started_at, last_activity_at, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sid, "foreground", "sonnet", started_at, started_at, "active"),
        )
    await db.commit()


class TestUsageAndStatus:
    async def test_under_limit_normal(self, tracker, db):
        # 5 sessions in last hour -> 25% -> NORMAL
        recent = (datetime(2026, 3, 11, 11, 30, 0, tzinfo=UTC)).isoformat()
        await _insert_sessions(db, 5, recent)
        assert await tracker.get_usage_pct() == pytest.approx(0.25)
        assert await tracker.get_status() == CCStatus.NORMAL

    async def test_at_80pct_throttled(self, tracker, db):
        recent = (datetime(2026, 3, 11, 11, 30, 0, tzinfo=UTC)).isoformat()
        await _insert_sessions(db, 16, recent)
        assert await tracker.get_usage_pct() == pytest.approx(0.80)
        assert await tracker.get_status() == CCStatus.THROTTLED

    async def test_at_100pct_rate_limited(self, tracker, db):
        recent = (datetime(2026, 3, 11, 11, 30, 0, tzinfo=UTC)).isoformat()
        await _insert_sessions(db, 20, recent)
        assert await tracker.get_usage_pct() == pytest.approx(1.0)
        assert await tracker.get_status() == CCStatus.RATE_LIMITED

    async def test_ignores_old_sessions(self, tracker, db):
        # Sessions from 2 hours ago should not count
        old = (datetime(2026, 3, 11, 9, 0, 0, tzinfo=UTC)).isoformat()
        await _insert_sessions(db, 20, old)
        assert await tracker.get_usage_pct() == pytest.approx(0.0)
        assert await tracker.get_status() == CCStatus.NORMAL


class TestThrottling:
    async def test_normal_nothing_throttled(self, tracker, db):
        recent = (datetime(2026, 3, 11, 11, 30, 0, tzinfo=UTC)).isoformat()
        await _insert_sessions(db, 5, recent)
        for p in [P_FOREGROUND, P_URGENT, P_REFLECTION, P_SCHEDULED, P_BACKGROUND, P_SURPLUS]:
            assert await tracker.should_throttle(p) is False

    async def test_throttled_blocks_p4_plus(self, tracker, db):
        recent = (datetime(2026, 3, 11, 11, 30, 0, tzinfo=UTC)).isoformat()
        await _insert_sessions(db, 16, recent)  # 80%
        assert await tracker.should_throttle(P_FOREGROUND) is False
        assert await tracker.should_throttle(P_URGENT) is False
        assert await tracker.should_throttle(P_REFLECTION) is False
        assert await tracker.should_throttle(P_SCHEDULED) is False
        assert await tracker.should_throttle(P_BACKGROUND) is True
        assert await tracker.should_throttle(P_SURPLUS) is True

    async def test_rate_limited_blocks_p2_plus(self, tracker, db):
        recent = (datetime(2026, 3, 11, 11, 30, 0, tzinfo=UTC)).isoformat()
        await _insert_sessions(db, 20, recent)  # 100%
        assert await tracker.should_throttle(P_FOREGROUND) is False
        assert await tracker.should_throttle(P_URGENT) is False
        assert await tracker.should_throttle(P_REFLECTION) is True
        assert await tracker.should_throttle(P_SCHEDULED) is True
        assert await tracker.should_throttle(P_BACKGROUND) is True

    async def test_foreground_never_throttled(self, tracker, db):
        recent = (datetime(2026, 3, 11, 11, 30, 0, tzinfo=UTC)).isoformat()
        await _insert_sessions(db, 30, recent)  # 150%
        assert await tracker.should_throttle(P_FOREGROUND) is False


class TestRecordSession:
    async def test_record_increases_count(self, tracker, db):
        assert await tracker.get_usage_pct() == pytest.approx(0.0)
        await tracker.record_session_start("foreground", P_FOREGROUND)
        assert await tracker.get_usage_pct() == pytest.approx(0.05)
