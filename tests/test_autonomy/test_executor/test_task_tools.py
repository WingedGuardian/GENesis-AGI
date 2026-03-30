"""Tests for genesis.mcp.health.task_tools (implementation functions)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from genesis.mcp.health import task_tools


@pytest.fixture(autouse=True)
def _reset_task_tools():
    """Reset module-level state before each test."""
    old_d, old_e, old_db = task_tools._dispatcher, task_tools._executor, task_tools._db
    task_tools._dispatcher = None
    task_tools._executor = None
    task_tools._db = None
    yield
    task_tools._dispatcher = old_d
    task_tools._executor = old_e
    task_tools._db = old_db


@pytest.mark.asyncio
class TestTaskSubmit:
    async def test_not_initialized(self) -> None:
        result = await task_tools._impl_task_submit("/path", "desc")
        assert "error" in result
        assert "not initialized" in result["error"]

    async def test_empty_plan_path(self) -> None:
        task_tools._dispatcher = AsyncMock()
        result = await task_tools._impl_task_submit("", "desc")
        assert "error" in result
        assert "required" in result["error"]

    async def test_successful_submit(self) -> None:
        dispatcher = AsyncMock()
        dispatcher.submit = AsyncMock(return_value="t-abc123")
        task_tools._dispatcher = dispatcher

        result = await task_tools._impl_task_submit("/path/plan.md", "Build X")
        assert result["task_id"] == "t-abc123"
        assert result["status"] == "dispatched"

    async def test_invalid_path_error(self) -> None:
        dispatcher = AsyncMock()
        dispatcher.submit = AsyncMock(side_effect=ValueError("bad path"))
        task_tools._dispatcher = dispatcher

        result = await task_tools._impl_task_submit("/bad/path", "task")
        assert "error" in result
        assert "Invalid" in result["error"]


@pytest.mark.asyncio
class TestTaskList:
    async def test_not_initialized(self) -> None:
        result = await task_tools._impl_task_list()
        assert "error" in result

    async def test_lists_active_tasks(self) -> None:
        task_tools._dispatcher = AsyncMock()
        task_tools._db = AsyncMock()

        with patch(
            "genesis.db.crud.task_states.list_active",
            new_callable=AsyncMock,
            return_value=[
                {"task_id": "t-001", "description": "Task 1", "current_phase": "executing", "created_at": "now"},
            ],
        ):
            result = await task_tools._impl_task_list()

        assert result["count"] == 1
        assert result["tasks"][0]["task_id"] == "t-001"


@pytest.mark.asyncio
class TestTaskDetail:
    async def test_not_found(self) -> None:
        task_tools._dispatcher = AsyncMock()
        task_tools._db = AsyncMock()

        with (
            patch("genesis.db.crud.task_states.get_by_id", new_callable=AsyncMock, return_value=None),
            patch("genesis.db.crud.task_steps.get_steps_for_task", new_callable=AsyncMock, return_value=[]),
        ):
            result = await task_tools._impl_task_detail("t-nonexistent")

        assert "error" in result
        assert "not found" in result["error"]


@pytest.mark.asyncio
class TestPauseResumeCancel:
    async def test_pause_not_initialized(self) -> None:
        result = await task_tools._impl_task_pause("t-001")
        assert "error" in result

    async def test_pause_success(self) -> None:
        executor = AsyncMock()
        executor.pause_task = lambda tid: True
        task_tools._executor = executor

        result = await task_tools._impl_task_pause("t-001")
        assert result["status"] == "pause_requested"

    async def test_resume_success(self) -> None:
        executor = AsyncMock()
        executor.resume_task = lambda tid: True
        task_tools._executor = executor

        result = await task_tools._impl_task_resume("t-001")
        assert result["status"] == "resumed"

    async def test_cancel_success(self) -> None:
        executor = AsyncMock()
        executor.cancel_task = lambda tid: True
        task_tools._executor = executor

        result = await task_tools._impl_task_cancel("t-001")
        assert result["status"] == "cancel_requested"

    async def test_cancel_not_found(self) -> None:
        executor = AsyncMock()
        executor.cancel_task = lambda tid: False
        task_tools._executor = executor

        result = await task_tools._impl_task_cancel("t-missing")
        assert "error" in result
