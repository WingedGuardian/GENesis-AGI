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
    monkeypatch.setattr(loop, "_last_pid_alert", None)
    create = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", create)
    return create


def _budget(status, pct, current=2000, maximum=2400, scope=None):
    b = {"status": status, "pct": pct, "current": current, "max": maximum}
    if scope is not None:
        b["scope"] = scope
    return b


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
async def test_service_scope_names_the_unit_remedy(_reset_cooldown_and_mock_create):
    # A service-scope binding (e.g. the server unit's own git/subprocess trees)
    # must name that unit and its TasksMax, not the generic user-slice remedy.
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(
        object(), budget=_budget("error", 92.0, 552, 600, scope="genesis-server.service")
    )
    content = create.await_args.kwargs["content"]
    assert "genesis-server.service" in content
    assert "TasksMax" in content
    assert "552/600" in content


@pytest.mark.asyncio
async def test_root_scope_says_container_wide(_reset_cooldown_and_mock_create):
    # A container-root binding is shared with system/other processes — the remedy
    # must NOT tell the user to raise a user-slice sub-cap that wouldn't help.
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(
        object(), budget=_budget("error", 95.0, 3800, 4000, scope="container-root")
    )
    content = create.await_args.kwargs["content"].lower()
    assert "container" in content
    assert "user-.slice tasksmax" not in content


@pytest.mark.asyncio
async def test_slice_scope_names_user_slice_remedy(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(
        object(), budget=_budget("degraded", 83.0, 2000, 2400, scope="user-1000.slice")
    )
    content = create.await_args.kwargs["content"]
    assert "TasksMax" in content
    assert "60%" in content  # the provisioned user-slice default


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
async def test_cross_scope_critical_not_suppressed(_reset_cooldown_and_mock_create):
    # A minor alert on ONE scope must NOT swallow a critical alert on a DIFFERENT
    # scope within the cooldown — that is the alert this monitor exists to raise.
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(
        db, budget=_budget("degraded", 82.0, 2000, 2400, scope="user-1000.slice")
    )
    await loop._check_pid_budget(
        db, budget=_budget("error", 95.0, 3800, 4000, scope="container-root")
    )
    assert create.await_count == 2  # scope changed AND escalated → both fire


@pytest.mark.asyncio
async def test_escalation_same_scope_not_suppressed(_reset_cooldown_and_mock_create):
    # degraded → error on the SAME scope inside the cooldown must still fire.
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(db, budget=_budget("degraded", 82.0, scope="user-1000.slice"))
    await loop._check_pid_budget(db, budget=_budget("error", 92.0, scope="user-1000.slice"))
    assert create.await_count == 2  # escalated degraded→error → not suppressed


@pytest.mark.asyncio
async def test_same_scope_same_severity_suppressed(_reset_cooldown_and_mock_create):
    # Same scope, same severity within the cooldown → suppressed (no alert storm).
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_pid_budget(db, budget=_budget("error", 95.0, scope="user-1000.slice"))
    await loop._check_pid_budget(db, budget=_budget("error", 96.0, scope="user-1000.slice"))
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_db_none_does_not_write_or_consume_cooldown(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_pid_budget(None, budget=_budget("error", 95.0))
    create.assert_not_awaited()
    assert loop._last_pid_alert is None  # cooldown not consumed
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
    monkeypatch.setattr(loop, "_last_pid_alert", None)
    await loop._check_pid_budget(object(), budget=None)  # no exception
