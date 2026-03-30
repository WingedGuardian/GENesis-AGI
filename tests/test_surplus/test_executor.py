"""Tests for StubExecutor."""

from __future__ import annotations

import pytest

from genesis.surplus.executor import StubExecutor
from genesis.surplus.types import ComputeTier, SurplusTask, TaskStatus, TaskType


def _make_task(task_type=TaskType.BRAINSTORM_USER, **kwargs):
    defaults = dict(
        id="st-1",
        task_type=task_type,
        compute_tier=ComputeTier.FREE_API,
        priority=0.5,
        drive_alignment="curiosity",
        status=TaskStatus.RUNNING,
        created_at="2026-03-04T10:00:00+00:00",
    )
    defaults.update(kwargs)
    return SurplusTask(**defaults)


@pytest.mark.asyncio
async def test_returns_success():
    result = await StubExecutor().execute(_make_task())
    assert result.success is True


@pytest.mark.asyncio
async def test_content_is_placeholder():
    result = await StubExecutor().execute(_make_task())
    assert "placeholder" in result.content.lower() or "stub" in result.content.lower()


@pytest.mark.asyncio
async def test_includes_task_type_in_content():
    task = _make_task(task_type=TaskType.BRAINSTORM_SELF)
    result = await StubExecutor().execute(task)
    assert "brainstorm_self" in result.content


@pytest.mark.asyncio
async def test_insights_list_populated():
    result = await StubExecutor().execute(_make_task())
    assert len(result.insights) >= 1
    for insight in result.insights:
        assert "content" in insight
        assert "drive_alignment" in insight


@pytest.mark.asyncio
async def test_no_error():
    result = await StubExecutor().execute(_make_task())
    assert result.error is None
