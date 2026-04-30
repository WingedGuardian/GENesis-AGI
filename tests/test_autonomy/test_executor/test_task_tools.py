"""Tests for genesis.mcp.health.task_tools (implementation functions)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from genesis.mcp.health import task_tools

# Minimal plan content satisfying REQUIRED_PLAN_SECTIONS validation
VALID_PLAN = (
    "# Test Plan\n"
    "## Requirements\nBuild it\n"
    "## Steps\n1. Do thing\n"
    "## Success Criteria\nIt works\n"
    "## Risks and Failure Modes\nNone significant\n"
)


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
    async def test_mcp_fallback_invalid_path(self) -> None:
        """Without dispatcher, MCP fallback validates plan path."""
        result = await task_tools._impl_task_submit("/path", "desc")
        assert "error" in result
        assert "outside allowed" in result["error"]

    async def test_mcp_fallback_missing_file(self) -> None:
        """Without dispatcher, MCP fallback checks file existence."""
        allowed_dir = Path.home() / ".claude" / "plans"
        result = await task_tools._impl_task_submit(
            str(allowed_dir / "nonexistent.md"), "desc",
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_mcp_fallback_creates_db_row(self, tmp_path: Path) -> None:
        """Without dispatcher, MCP fallback creates DB row directly."""
        plan = tmp_path / "test-plan.md"
        plan.write_text(VALID_PLAN)

        # Temporarily allow the tmp dir
        original_dirs = task_tools._ALLOWED_PLAN_DIRS[:]
        task_tools._ALLOWED_PLAN_DIRS.append(tmp_path)

        mock_db = AsyncMock()
        mock_db.close = AsyncMock()
        mock_db.commit = AsyncMock()

        with (
            patch.object(task_tools, "_get_db", new_callable=AsyncMock, return_value=mock_db),
            patch("genesis.db.crud.task_states.create", new_callable=AsyncMock),
        ):
            result = await task_tools._impl_task_submit(str(plan), "Test task")

        task_tools._ALLOWED_PLAN_DIRS[:] = original_dirs

        assert "task_id" in result
        assert result["status"] == "pending"
        assert "dispatch cycle" in result.get("note", "")

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
        assert "bad path" in result["error"]

    async def test_mcp_fallback_rejects_missing_sections(self, tmp_path: Path) -> None:
        """MCP fallback validates plan content after path checks pass."""
        plan = tmp_path / "incomplete.md"
        plan.write_text("# Plan\n## Steps\n1. Do thing\n")

        original_dirs = task_tools._ALLOWED_PLAN_DIRS[:]
        task_tools._ALLOWED_PLAN_DIRS.append(tmp_path)

        result = await task_tools._impl_task_submit(str(plan), "Bad task")

        task_tools._ALLOWED_PLAN_DIRS[:] = original_dirs

        assert "error" in result
        assert "missing required sections" in result["error"]
        assert "## Requirements" in result["error"]
        assert "## Success Criteria" in result["error"]

    async def test_mcp_fallback_accepts_valid_plan(self, tmp_path: Path) -> None:
        """MCP fallback passes content validation for well-formed plans."""
        plan = tmp_path / "good.md"
        plan.write_text(VALID_PLAN)

        original_dirs = task_tools._ALLOWED_PLAN_DIRS[:]
        task_tools._ALLOWED_PLAN_DIRS.append(tmp_path)

        mock_db = AsyncMock()
        mock_db.close = AsyncMock()

        with (
            patch.object(task_tools, "_get_db", new_callable=AsyncMock, return_value=mock_db),
            patch("genesis.db.crud.task_states.create", new_callable=AsyncMock),
        ):
            result = await task_tools._impl_task_submit(str(plan), "Good task")

        task_tools._ALLOWED_PLAN_DIRS[:] = original_dirs

        assert "task_id" in result
        assert result["status"] == "pending"


@pytest.mark.asyncio
class TestTaskList:
    async def test_db_fallback_on_connection_error(self) -> None:
        """Without wired _db, tries _get_db() fallback."""
        with patch.object(
            task_tools, "_get_db",
            new_callable=AsyncMock,
            side_effect=Exception("no DB"),
        ):
            result = await task_tools._impl_task_list()
        assert "error" in result
        assert "unavailable" in result["error"].lower()

    async def test_db_fallback_lists_tasks(self) -> None:
        """Without wired _db, opens own connection and lists."""
        mock_db = AsyncMock()
        mock_db.close = AsyncMock()

        with (
            patch.object(task_tools, "_get_db", new_callable=AsyncMock, return_value=mock_db),
            patch(
                "genesis.db.crud.task_states.list_active",
                new_callable=AsyncMock,
                return_value=[
                    {"task_id": "t-001", "description": "Task 1", "current_phase": "executing", "created_at": "now"},
                ],
            ),
        ):
            result = await task_tools._impl_task_list()

        assert result["count"] == 1
        assert result["tasks"][0]["task_id"] == "t-001"
        mock_db.close.assert_awaited_once()

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

    async def test_db_fallback_detail(self) -> None:
        """Without wired _db, opens own connection for detail."""
        mock_db = AsyncMock()
        mock_db.close = AsyncMock()

        with (
            patch.object(task_tools, "_get_db", new_callable=AsyncMock, return_value=mock_db),
            patch(
                "genesis.db.crud.task_states.get_by_id",
                new_callable=AsyncMock,
                return_value={
                    "task_id": "t-002",
                    "description": "Test",
                    "current_phase": "pending",
                    "created_at": "now",
                    "updated_at": "now",
                    "blockers": None,
                    "outputs": None,
                },
            ),
            patch(
                "genesis.db.crud.task_steps.get_steps_for_task",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await task_tools._impl_task_detail("t-002")

        assert result["task_id"] == "t-002"
        mock_db.close.assert_awaited_once()


@pytest.mark.asyncio
class TestTaskControl:
    async def test_not_initialized(self) -> None:
        result = await task_tools._impl_task_control("t-001", "pause")
        assert "error" in result
        assert "main Genesis server" in result["error"]

    async def test_invalid_action(self) -> None:
        task_tools._executor = AsyncMock()
        result = await task_tools._impl_task_control("t-001", "explode")
        assert "error" in result
        assert "Invalid action" in result["error"]

    async def test_pause_success(self) -> None:
        executor = AsyncMock()
        executor.pause_task = lambda tid: True
        task_tools._executor = executor

        result = await task_tools._impl_task_control("t-001", "pause")
        assert result["status"] == "pause_requested"

    async def test_resume_success(self) -> None:
        executor = AsyncMock()
        executor.resume_task = lambda tid: True
        task_tools._executor = executor

        result = await task_tools._impl_task_control("t-001", "resume")
        assert result["status"] == "resumed"

    async def test_cancel_success(self) -> None:
        executor = AsyncMock()
        executor.cancel_task = lambda tid: True
        task_tools._executor = executor

        result = await task_tools._impl_task_control("t-001", "cancel")
        assert result["status"] == "cancel_requested"

    async def test_cancel_not_found(self) -> None:
        executor = AsyncMock()
        executor.cancel_task = lambda tid: False
        task_tools._executor = executor

        result = await task_tools._impl_task_control("t-missing", "cancel")
        assert "error" in result
