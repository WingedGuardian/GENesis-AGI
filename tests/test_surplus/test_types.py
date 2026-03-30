"""Tests for genesis.surplus.types."""

import pytest

from genesis.surplus.types import (
    ComputeTier,
    ExecutorResult,
    SurplusExecutor,
    SurplusTask,
    TaskStatus,
    TaskType,
)


class TestTaskType:
    def test_task_type_values(self) -> None:
        assert TaskType.BRAINSTORM_USER == "brainstorm_user"
        assert TaskType.BRAINSTORM_SELF == "brainstorm_self"
        assert TaskType.META_BRAINSTORM == "meta_brainstorm"
        assert TaskType.MEMORY_AUDIT == "memory_audit"
        assert TaskType.SELF_UNBLOCK == "self_unblock"


class TestComputeTier:
    def test_compute_tier_values(self) -> None:
        assert ComputeTier.LOCAL_30B == "local_30b"
        assert ComputeTier.FREE_API == "free_api"
        assert ComputeTier.CHEAP_PAID == "cheap_paid"
        assert ComputeTier.NEVER == "never"


class TestTaskStatus:
    def test_task_status_values(self) -> None:
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"
        assert TaskStatus.CANCELLED == "cancelled"


class TestSurplusTask:
    def test_surplus_task_creation(self) -> None:
        task = SurplusTask(
            id="t1",
            task_type=TaskType.BRAINSTORM_USER,
            compute_tier=ComputeTier.LOCAL_30B,
            priority=0.8,
            drive_alignment="curiosity",
            status=TaskStatus.PENDING,
            created_at="2026-03-04T00:00:00Z",
        )
        assert task.id == "t1"
        assert task.payload is None
        assert task.attempt_count == 0

    def test_surplus_task_is_frozen(self) -> None:
        task = SurplusTask(
            id="t1",
            task_type=TaskType.BRAINSTORM_USER,
            compute_tier=ComputeTier.LOCAL_30B,
            priority=0.8,
            drive_alignment="curiosity",
            status=TaskStatus.PENDING,
            created_at="2026-03-04T00:00:00Z",
        )
        with pytest.raises(AttributeError):
            task.status = TaskStatus.RUNNING  # type: ignore[misc]


class TestExecutorResult:
    def test_executor_result_success(self) -> None:
        result = ExecutorResult(success=True)
        assert result.success is True
        assert result.content is None
        assert result.insights == []
        assert result.error is None

    def test_executor_result_failure(self) -> None:
        result = ExecutorResult(success=False, error="something broke")
        assert result.success is False
        assert result.error == "something broke"


class TestSurplusExecutor:
    def test_executor_protocol_has_execute(self) -> None:
        assert "execute" in dir(SurplusExecutor)
