"""Tests for genesis.autonomy.dispatcher.TaskDispatcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from genesis.autonomy.dispatcher import TaskDispatcher, _validate_plan_path

# ---------------------------------------------------------------------------
# Path validation tests
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_valid_claude_plans_path(self, tmp_path: Path) -> None:
        plan = tmp_path / "test.md"
        plan.write_text("# Plan")
        with patch(
            "genesis.autonomy.dispatcher._ALLOWED_PLAN_DIRS",
            [tmp_path],
        ):
            result = _validate_plan_path(str(plan))
            assert result == plan.resolve()

    def test_rejects_outside_path(self, tmp_path: Path) -> None:
        plan = tmp_path / "evil.md"
        plan.write_text("# Evil")
        with patch(
            "genesis.autonomy.dispatcher._ALLOWED_PLAN_DIRS",
            [Path("/nonexistent/allowed")],
        ), pytest.raises(ValueError, match="outside allowed"):
            _validate_plan_path(str(plan))

    def test_rejects_missing_file(self) -> None:
        with patch(
            "genesis.autonomy.dispatcher._ALLOWED_PLAN_DIRS",
            [Path.home()],
        ), pytest.raises(FileNotFoundError):
            _validate_plan_path(str(Path.home() / "nonexistent_plan_xyz.md"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_executor() -> AsyncMock:
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value=True)
    return executor


@pytest.fixture
def dispatcher(mock_executor: AsyncMock) -> TaskDispatcher:
    return TaskDispatcher(
        db=AsyncMock(),
        executor=mock_executor,
        event_bus=None,
    )


# ---------------------------------------------------------------------------
# Submit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSubmit:
    async def test_submit_creates_task(
        self, dispatcher: TaskDispatcher, tmp_path: Path,
    ) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n## Steps\n1. Do thing")

        with (
            patch("genesis.autonomy.dispatcher._ALLOWED_PLAN_DIRS", [tmp_path]),
            patch("genesis.db.crud.task_states.create", new_callable=AsyncMock) as mock_create,
            patch("genesis.util.tasks.tracked_task"),
        ):
            task_id = await dispatcher.submit(str(plan), "Test task")

        assert task_id.startswith("t-")
        assert task_id in dispatcher._dispatched
        mock_create.assert_awaited_once()

    async def test_submit_invalid_path(self, dispatcher: TaskDispatcher) -> None:
        with pytest.raises(ValueError, match="outside allowed"):
            await dispatcher.submit("/etc/passwd", "Bad task")

    async def test_submit_missing_file(
        self, dispatcher: TaskDispatcher, tmp_path: Path,
    ) -> None:
        with patch(
            "genesis.autonomy.dispatcher._ALLOWED_PLAN_DIRS",
            [tmp_path],
        ), pytest.raises(FileNotFoundError):
            await dispatcher.submit(
                str(tmp_path / "gone.md"), "Missing plan",
            )

    async def test_submit_with_event_bus(
        self, mock_executor: AsyncMock, tmp_path: Path,
    ) -> None:
        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()
        d = TaskDispatcher(db=AsyncMock(), executor=mock_executor, event_bus=event_bus)

        plan = tmp_path / "plan.md"
        plan.write_text("# Plan")

        with (
            patch("genesis.autonomy.dispatcher._ALLOWED_PLAN_DIRS", [tmp_path]),
            patch("genesis.db.crud.task_states.create", new_callable=AsyncMock),
            patch("genesis.util.tasks.tracked_task"),
        ):
            await d.submit(str(plan), "With events")

        event_bus.emit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Dispatch cycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDispatchCycle:
    async def test_picks_up_observations(
        self, dispatcher: TaskDispatcher, tmp_path: Path,
    ) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan")

        obs = {
            "id": "obs-1",
            "content": "Build feature Y",
            "metadata": {"plan_path": str(plan)},
        }

        with (
            patch("genesis.autonomy.dispatcher._ALLOWED_PLAN_DIRS", [tmp_path]),
            patch("genesis.db.crud.observations.query", new_callable=AsyncMock, return_value=[obs]),
            patch("genesis.db.crud.observations.resolve", new_callable=AsyncMock) as mock_resolve,
            patch("genesis.db.crud.task_states.create", new_callable=AsyncMock),
            patch("genesis.db.crud.task_states.list_active", new_callable=AsyncMock, return_value=[]),
            patch("genesis.util.tasks.tracked_task"),
        ):
            count = await dispatcher.dispatch_cycle()

        assert count == 1
        mock_resolve.assert_awaited()

    async def test_dedup_skips_active_tasks(
        self, dispatcher: TaskDispatcher,
    ) -> None:
        obs = {
            "id": "obs-2",
            "content": "Already running",
            "metadata": {"plan_path": "/some/path"},
        }

        with (
            patch("genesis.db.crud.observations.query", new_callable=AsyncMock, return_value=[obs]),
            patch("genesis.db.crud.observations.resolve", new_callable=AsyncMock) as mock_resolve,
            patch("genesis.db.crud.task_states.list_active", new_callable=AsyncMock, return_value=[
                {"task_id": "t-existing", "description": "Already running"},
            ]),
        ):
            count = await dispatcher.dispatch_cycle()

        assert count == 0
        mock_resolve.assert_awaited_once()

    async def test_skips_obs_without_plan_path(
        self, dispatcher: TaskDispatcher,
    ) -> None:
        obs = {"id": "obs-3", "content": "No plan", "metadata": None}

        with (
            patch("genesis.db.crud.observations.query", new_callable=AsyncMock, return_value=[obs]),
            patch("genesis.db.crud.task_states.list_active", new_callable=AsyncMock, return_value=[]),
        ):
            count = await dispatcher.dispatch_cycle()

        assert count == 0


# ---------------------------------------------------------------------------
# Recovery tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecoverIncomplete:
    async def test_recovers_executing_task(
        self, dispatcher: TaskDispatcher,
    ) -> None:
        with (
            patch("genesis.db.crud.task_states.list_active", new_callable=AsyncMock, return_value=[
                {"task_id": "t-exec", "current_phase": "executing"},
            ]),
            patch("genesis.util.tasks.tracked_task") as mock_tt,
        ):
            count = await dispatcher.recover_incomplete()

        assert count == 1
        assert "t-exec" in dispatcher._dispatched
        mock_tt.assert_called_once()

    async def test_handles_blocked_task(
        self, dispatcher: TaskDispatcher,
    ) -> None:
        with patch("genesis.db.crud.task_states.list_active", new_callable=AsyncMock, return_value=[
            {"task_id": "t-blocked", "current_phase": "blocked"},
        ]):
            count = await dispatcher.recover_incomplete()

        assert count == 1
        assert "t-blocked" in dispatcher._dispatched

    async def test_skips_terminal_tasks(
        self, dispatcher: TaskDispatcher,
    ) -> None:
        with patch("genesis.db.crud.task_states.list_active", new_callable=AsyncMock, return_value=[
            {"task_id": "t-done", "current_phase": "completed"},
        ]):
            count = await dispatcher.recover_incomplete()

        assert count == 0
