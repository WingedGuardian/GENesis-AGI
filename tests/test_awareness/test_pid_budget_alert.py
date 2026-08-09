"""Tests for _check_pid_budget — the per-user-slice PID/task-budget alert.

The task budget maxes under many concurrent CC sessions (each spawns MCP
subprocess trees) and precedes `Cannot fork` while memory/cpu/disk read green.
degraded (>=80%) -> 'high'; error (>=90%) -> 'critical'. The alert is
EXPLANATORY: it names the sub-cap and the remedy so a cryptic `Cannot fork`
becomes actionable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop


@pytest.fixture(autouse=True)
def _reset_cooldown_and_mock_create(monkeypatch):
    monkeypatch.setattr(loop, "_last_pid_budget_alert_at", None)
    create = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", create)
    return create


def _budget(status, pct, current=2000, maximum=2400):
    return {"status": status, "pct": pct, "current": current, "max": maximum}


@pytest.mark.asyncio
async def test_error_creates_critical_explanatory_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget=_budget("error", 95.0, 2280, 2400))
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["type"] == "infrastructure_alert"
    assert kwargs["priority"] == "critical"
    assert kwargs["source"] == "pid_budget_monitor"
    # explanatory: names the failure mode and the remedy
    assert "Cannot fork" in kwargs["content"]
    assert "TasksMax" in kwargs["content"]
    assert "2280/2400" in kwargs["content"]


@pytest.mark.asyncio
async def test_degraded_is_high_priority(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget=_budget("degraded", 83.0))
    create.assert_awaited_once()
    assert create.await_args.kwargs["priority"] == "high"


@pytest.mark.asyncio
async def test_healthy_no_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget=_budget("healthy", 41.0))
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_no_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(object(), budget={"status": "unavailable"})
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_subcap_max_sentinel_no_alert(_reset_cooldown_and_mock_create):
    # pids.max == "max" surfaces as healthy/pct None → must never alarm.
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(
        object(), budget={"status": "healthy", "current": 1500, "max": "max", "pct": None}
    )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(db, budget=_budget("error", 95.0))
    await loop._check_pid_budget(db, budget=_budget("error", 96.0))
    create.assert_awaited_once()  # second is within the 1h cooldown


@pytest.mark.asyncio
async def test_db_none_does_not_write_or_consume_cooldown(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(None, budget=_budget("error", 95.0))
    create.assert_not_awaited()
    assert loop._last_pid_budget_alert_at is None  # cooldown not consumed
    await loop._check_pid_budget(object(), budget=_budget("error", 95.0))
    create.assert_awaited_once()  # a later tick with a live db still alerts


@pytest.mark.asyncio
async def test_create_failure_never_raises(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    create.side_effect = RuntimeError("db locked")
    await loop._check_pid_budget(object(), budget=_budget("error", 95.0))  # must not propagate


@pytest.mark.asyncio
async def test_budget_none_reads_live_without_raising(monkeypatch):
    # When budget is not injected it reads the live cgroup; on any host it must
    # return without raising (healthy/unavailable → no alert).
    create = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", create)
    monkeypatch.setattr(loop, "_last_pid_budget_alert_at", None)
    await loop._check_pid_budget(object(), budget=None)  # no exception
