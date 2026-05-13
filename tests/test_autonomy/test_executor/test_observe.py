"""Tests for genesis.autonomy.executor.observe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor.observe import (
    COMMIT_WARN_THRESHOLD,
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
# Activity age checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestActivityAge:
    async def test_fresh_task_no_annotation(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            updated_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert not result.annotations

    async def test_48h_stale_annotates(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            updated_at=_ts(STALE_WARN_HOURS + 1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert any("activity" in a.lower() or "no activity" in a.lower()
                   for a in result.annotations)

    async def test_7d_stale_annotates_not_blocks(self, monkeypatch, tmp_path):
        """Even very stale tasks only get annotated, never blocked."""
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            updated_at=_ts(170), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        # Should have an annotation about staleness
        assert len(result.annotations) > 0
        assert any("days" in a or "activity" in a.lower() for a in result.annotations)

    async def test_empty_updated_at_no_annotation(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            updated_at="", repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert not result.annotations

    async def test_invalid_updated_at_no_annotation(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            updated_at="not-a-date", repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert not result.annotations


# ---------------------------------------------------------------------------
# Git activity checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGitActivity:
    async def test_low_activity_no_annotation(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=5)
        result = await observe(
            updated_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert not any("commit" in a.lower() for a in result.annotations)

    async def test_moderate_activity_annotates(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=COMMIT_WARN_THRESHOLD + 1)
        result = await observe(
            updated_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert any("commit" in a.lower() for a in result.annotations)

    async def test_high_activity_annotates_not_blocks(self, monkeypatch, tmp_path):
        """Even 100+ commits only annotates, never blocks."""
        _mock_git(monkeypatch, commit_count=100)
        result = await observe(
            updated_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert any("commit" in a.lower() for a in result.annotations)

    async def test_git_failure_is_failopen(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0, fail=True)
        result = await observe(
            updated_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert not any("commit" in a.lower() for a in result.annotations)


# ---------------------------------------------------------------------------
# Task overlap checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTaskOverlap:
    async def test_no_overlap_no_annotation(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        result = await observe(
            updated_at=_ts(1), repo_root=tmp_path,
            active_tasks=[], task_id="t-001",
        )
        assert not any("other task" in a.lower() for a in result.annotations)

    async def test_completed_task_overlap_annotates(self, monkeypatch, tmp_path):
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
            updated_at=_ts(2), repo_root=tmp_path,
            active_tasks=other_tasks, task_id="t-001",
        )
        assert any("other task" in a.lower() for a in result.annotations)

    async def test_self_excluded_from_overlap(self, monkeypatch, tmp_path):
        _mock_git(monkeypatch, commit_count=0)
        other_tasks = [
            {
                "task_id": "t-001",
                "current_phase": "completed",
                "updated_at": datetime.now(UTC).isoformat(),
                "created_at": _ts(3),
            },
        ]
        result = await observe(
            updated_at=_ts(2), repo_root=tmp_path,
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
            updated_at=_ts(2), repo_root=tmp_path,
            active_tasks=other_tasks, task_id="t-001",
        )
        assert not any("other task" in a.lower() for a in result.annotations)
