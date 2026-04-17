"""Tests for CostTracker."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from genesis.db.crud import cost_events as cost_events_crud
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.types import BudgetStatus, CallResult


@pytest.fixture
def clock():
    """Fixed clock for deterministic tests."""
    # Wednesday 2026-03-04 12:00:00 UTC
    now = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
    return lambda: now


@pytest.fixture
def tracker(db, clock):
    return CostTracker(db, clock=clock)


@pytest.mark.asyncio
async def test_record_creates_cost_event(tracker, db):
    result = CallResult(success=True, input_tokens=100, output_tokens=50, cost_usd=0.01)
    await tracker.record("2_triage", "anthropic", result)

    events = await cost_events_crud.query(db, event_type="llm_call")
    assert len(events) == 1
    assert events[0]["cost_usd"] == 0.01
    assert events[0]["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_check_budget_under_limit(tracker):
    status = await tracker.check_budget()
    assert status == BudgetStatus.UNDER_LIMIT


@pytest.mark.asyncio
async def test_check_budget_warning(tracker, db):
    # Daily budget is $2, warning at 80% = $1.60
    result = CallResult(success=True, cost_usd=1.70)
    await tracker.record("2_triage", "anthropic", result)
    status = await tracker.check_budget()
    assert status == BudgetStatus.WARNING


@pytest.mark.asyncio
async def test_check_budget_exceeded(tracker, db):
    # Daily budget is $2
    result = CallResult(success=True, cost_usd=2.50)
    await tracker.record("2_triage", "anthropic", result)
    status = await tracker.check_budget()
    assert status == BudgetStatus.EXCEEDED


@pytest.mark.asyncio
async def test_get_period_cost(tracker):
    result = CallResult(success=True, cost_usd=0.05)
    await tracker.record("2_triage", "anthropic", result)
    await tracker.record("3_micro_reflection", "anthropic", result)

    cost = await tracker.get_period_cost("today")
    assert cost == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_period_start_today(clock):
    tracker = CostTracker.__new__(CostTracker)
    tracker._clock = clock
    start = tracker._period_start("today")
    assert start == "2026-03-04T00:00:00+00:00"


@pytest.mark.asyncio
async def test_period_start_this_week(clock):
    tracker = CostTracker.__new__(CostTracker)
    tracker._clock = clock
    # 2026-03-04 is Wednesday, Monday is 2026-03-02
    start = tracker._period_start("this_week")
    assert start == "2026-03-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_period_start_this_month(clock):
    tracker = CostTracker.__new__(CostTracker)
    tracker._clock = clock
    start = tracker._period_start("this_month")
    assert start == "2026-03-01T00:00:00+00:00"


class TestBudgetEventThrottle:
    """Verify budget events are throttled but status is always returned."""

    @pytest.mark.asyncio
    async def test_exceeded_event_emitted_once_on_rapid_calls(self, db, clock):
        bus = AsyncMock()
        tracker = CostTracker(db, clock=clock, event_bus=bus)

        # Push past daily budget
        await tracker.record("2_triage", "anthropic", CallResult(success=True, cost_usd=3.00))

        # Call check_budget 10 times rapidly
        for _ in range(10):
            status = await tracker.check_budget()
            assert status == BudgetStatus.EXCEEDED  # status always returned

        # Event emitted exactly once (first call only)
        exceeded_calls = [
            c for c in bus.emit.call_args_list
            if c.args[2] == "budget.exceeded"
        ]
        assert len(exceeded_calls) == 1

    @pytest.mark.asyncio
    async def test_warning_event_emitted_once_on_rapid_calls(self, db, clock):
        bus = AsyncMock()
        tracker = CostTracker(db, clock=clock, event_bus=bus)

        # Push into warning zone (daily $2, 80% = $1.60)
        await tracker.record("2_triage", "anthropic", CallResult(success=True, cost_usd=1.70))

        for _ in range(10):
            status = await tracker.check_budget()
            assert status == BudgetStatus.WARNING

        warning_calls = [
            c for c in bus.emit.call_args_list
            if c.args[2] == "budget.warning"
        ]
        assert len(warning_calls) == 1

    @pytest.mark.asyncio
    async def test_event_re_emits_in_new_period(self, db):
        """Event emits again when the budget period rolls over."""
        bus = AsyncMock()
        # Day 1
        day1 = datetime(2026, 3, 4, 14, 0, 0, tzinfo=UTC)
        tracker = CostTracker(db, clock=lambda: day1, event_bus=bus)
        await tracker.record("2_triage", "anthropic", CallResult(success=True, cost_usd=3.00))
        await tracker.check_budget()

        # Day 2 — new period boundary, should emit again
        day2 = datetime(2026, 3, 5, 10, 0, 0, tzinfo=UTC)
        tracker._clock = lambda: day2
        await tracker.record("2_triage", "anthropic", CallResult(success=True, cost_usd=3.00))
        await tracker.check_budget()

        exceeded_calls = [
            c for c in bus.emit.call_args_list
            if c.args[2] == "budget.exceeded"
        ]
        assert len(exceeded_calls) == 2  # once per day

    @pytest.mark.asyncio
    async def test_no_event_bus_still_returns_status(self, db, clock):
        """Without event_bus, status still works (no emit, no error)."""
        tracker = CostTracker(db, clock=clock, event_bus=None)
        await tracker.record("2_triage", "anthropic", CallResult(success=True, cost_usd=3.00))
        status = await tracker.check_budget()
        assert status == BudgetStatus.EXCEEDED
