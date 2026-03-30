"""Tests for CCReflectionBridge resilience integration (throttling/deferral)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.awareness.types import Depth, TickResult
from genesis.cc.reflection_bridge import CCReflectionBridge
from genesis.resilience.deferred_work import DeferredWorkQueue


@pytest.fixture
def mock_bridge():
    return CCReflectionBridge(
        session_manager=AsyncMock(),
        invoker=AsyncMock(),
        db=AsyncMock(),
        event_bus=None,
    )


@pytest.fixture
def mock_budget():
    budget = AsyncMock()
    budget.should_throttle = AsyncMock(return_value=False)
    return budget


@pytest.fixture
def mock_tick():
    return TickResult(
        tick_id="t1",
        timestamp=datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC).isoformat(),
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=None,
        trigger_reason="test",
    )


class TestReflectThrottling:
    async def test_throttled_returns_deferred(self, mock_bridge, mock_tick, db):
        budget = AsyncMock()
        budget.should_throttle = AsyncMock(return_value=True)
        mock_bridge.set_cc_budget(budget)

        clock = lambda: datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)  # noqa: E731
        queue = DeferredWorkQueue(db, clock=clock)
        mock_bridge.set_deferred_queue(queue)

        result = await mock_bridge.reflect(Depth.DEEP, mock_tick, db=db)
        assert result.success is False
        assert "throttled" in result.reason.lower()

        # Verify work was enqueued with correct staleness policy
        count = await queue.count_pending()
        assert count == 1

        # Verify TTL staleness policy (not "refresh" which expires instantly)
        from genesis.db.crud import deferred_work as dw_crud
        items = await dw_crud.query_pending(db)
        assert items[0]["staleness_policy"] == "ttl"
        assert items[0]["staleness_ttl_s"] == 14400

    async def test_not_throttled_proceeds(self, mock_bridge, mock_budget, mock_tick, db):
        mock_bridge.set_cc_budget(mock_budget)

        # Set up session manager and invoker for normal flow
        sess = {"id": "s1"}
        mock_bridge._session_manager.create_background = AsyncMock(return_value=sess)

        output = MagicMock()
        output.is_error = False
        output.text = "reflection output"
        output.cost_usd = 0.01
        output.input_tokens = 100
        output.output_tokens = 50
        output.model_used = "sonnet"
        mock_bridge._invoker.run = AsyncMock(return_value=output)

        result = await mock_bridge.reflect(Depth.DEEP, mock_tick, db=db)
        assert result.success is True

    async def test_no_budget_normal_behavior(self, mock_bridge, mock_tick, db):
        # No cc_budget set — should proceed normally
        sess = {"id": "s1"}
        mock_bridge._session_manager.create_background = AsyncMock(return_value=sess)

        output = MagicMock()
        output.is_error = False
        output.text = "output"
        output.cost_usd = 0.0
        output.input_tokens = 10
        output.output_tokens = 10
        output.model_used = "sonnet"
        mock_bridge._invoker.run = AsyncMock(return_value=output)

        result = await mock_bridge.reflect(Depth.DEEP, mock_tick, db=db)
        assert result.success is True


class TestWeeklyThrottling:
    async def test_weekly_assessment_throttled(self, mock_bridge, db):
        budget = AsyncMock()
        budget.should_throttle = AsyncMock(return_value=True)
        mock_bridge.set_cc_budget(budget)

        result = await mock_bridge.run_weekly_assessment(db)
        assert result.success is False
        assert "throttled" in result.reason.lower()

    async def test_quality_calibration_throttled(self, mock_bridge, db):
        budget = AsyncMock()
        budget.should_throttle = AsyncMock(return_value=True)
        mock_bridge.set_cc_budget(budget)

        result = await mock_bridge.run_quality_calibration(db)
        assert result.success is False
        assert "throttled" in result.reason.lower()
