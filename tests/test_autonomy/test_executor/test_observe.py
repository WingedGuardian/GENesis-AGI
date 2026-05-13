"""Tests for genesis.autonomy.executor.observe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor.observe import (
    COMMIT_BLOCK_THRESHOLD,
    COMMIT_WARN_THRESHOLD,
    STALE_BLOCK_HOURS,
    STALE_WARN_HOURS,
    observe,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_git(monkeypatch, *, commit_count: int, fail: bool = False):
    """Mock asyncio.create_subprocess_exec for git log."""
    proc = AsyncMock()
    if fail:
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
    else:
        if commit_count > 0:
            lines = "\n".join(
                f"abc{i:04d} commit {i}" for i in range(commit_count)
            )
            stdout = lines.encode()
        else:
            stdout = b""
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(stdout, b""))

    monkeypatch.setattr(
        "genesis.autonomy.executor.observe.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    )


def _ts(hours_ago: float) -> str:
    """Return ISO timestamp for N hours ago."""
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


# ---------------------------------------------------------------------------
# Plan age checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPlanAge:
    async def test_fresh_task_proceeds(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            created_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is True
        assert not any("stale" in a.lower() or "old" in a.lower()
                       for a in result.annotations)

    async def test_48h_task_warns(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            created_at=_ts(STALE_WARN_HOURS + 1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is True
        assert any("old" in a.lower() for a in result.annotations)

    async def test_7d_task_blocks(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            created_at=_ts(STALE_BLOCK_HOURS + 1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is False
        assert result.block_reason is not None
        assert "days" in result.block_reason.lower()

    async def test_empty_created_at_proceeds(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            created_at="", repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is True

    async def test_invalid_created_at_proceeds(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            created_at="not-a-date", repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is True


# ---------------------------------------------------------------------------
# Git activity checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGitActivity:
    async def test_low_activity_proceeds(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=5)
        result = await observe(
            created_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is True
        assert not any("commit" in a.lower() for a in result.annotations)

    async def test_moderate_activity_warns(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=COMMIT_WARN_THRESHOLD + 1)
        result = await observe(
            created_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is True
        assert any("commit" in a.lower() for a in result.annotations)

    async def test_extreme_activity_blocks(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=COMMIT_BLOCK_THRESHOLD + 1)
        result = await observe(
            created_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is False
        assert "commit" in result.block_reason.lower()

    async def test_git_failure_is_failopen(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0, fail=True)
        result = await observe(
            created_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is True


# ---------------------------------------------------------------------------
# Task overlap checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTaskOverlap:
    async def test_no_overlap_no_warning(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            created_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert result.proceed is True
        assert not any("other task" in a.lower() for a in result.annotations)

    async def test_completed_task_overlap_warns(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        other_tasks = [
            {
                "task_id": "t-other",
                "current_phase": "completed",
                "updated_at": datetime.now(UTC).isoformat(),
                "created_at": _ts(3),
            },
        ]
        result = await observe(
            created_at=_ts(2), repo_root=tmp_path,
            active_tasks=other_tasks, task_id="t-001",
        )
        assert result.proceed is True
        assert any("other task" in a.lower() for a in result.annotations)

    async def test_self_excluded_from_overlap(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        other_tasks = [
            {
                "task_id": "t-001",  # same task
                "current_phase": "completed",
                "updated_at": datetime.now(UTC).isoformat(),
                "created_at": _ts(3),
            },
        ]
        result = await observe(
            created_at=_ts(2), repo_root=tmp_path,
            active_tasks=other_tasks, task_id="t-001",
        )
        assert not any("other task" in a.lower() for a in result.annotations)

    async def test_active_tasks_not_counted(self, monkeypatch, tmp_path):
        """Only completed/failed tasks trigger overlap, not active ones."""
        _mock_git(monkeypatch, commit_count=0)
        other_tasks = [
            {
                "task_id": "t-active",
                "current_phase": "executing",
                "updated_at": datetime.now(UTC).isoformat(),
                "created_at": _ts(3),
            },
        ]
        result = await observe(
            created_at=_ts(2), repo_root=tmp_path,
            active_tasks=other_tasks, task_id="t-001",
        )
        assert not any("other task" in a.lower() for a in result.annotations)
