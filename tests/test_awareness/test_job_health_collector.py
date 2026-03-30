"""Tests for JobHealthCollector signal collector."""

from __future__ import annotations

import pytest

from genesis.awareness.signals import JobHealthCollector


class _MockRuntime:
    """Minimal runtime mock exposing job_health."""

    def __init__(self, health: dict | None = None):
        self._health = health or {}

    @property
    def job_health(self) -> dict:
        return self._health


@pytest.mark.asyncio
async def test_no_runtime_returns_stub():
    collector = JobHealthCollector(runtime=None)
    reading = await collector.collect()
    assert reading.value == 0.0
    assert reading.source == "runtime"


@pytest.mark.asyncio
async def test_empty_job_health():
    collector = JobHealthCollector(runtime=_MockRuntime({}))
    reading = await collector.collect()
    assert reading.value == 0.0


@pytest.mark.asyncio
async def test_all_jobs_healthy():
    health = {
        "weekly_assessment": {"consecutive_failures": 0},
        "surplus_tick": {"consecutive_failures": 1},
    }
    collector = JobHealthCollector(runtime=_MockRuntime(health), failure_threshold=2)
    reading = await collector.collect()
    assert reading.value == 0.0  # 1 failure < threshold of 2


@pytest.mark.asyncio
async def test_job_above_threshold():
    health = {
        "weekly_assessment": {"consecutive_failures": 3},
        "surplus_tick": {"consecutive_failures": 0},
    }
    collector = JobHealthCollector(runtime=_MockRuntime(health), failure_threshold=2)
    reading = await collector.collect()
    assert reading.value == 0.75  # 3 / (2 * 2) = 0.75


@pytest.mark.asyncio
async def test_value_clamped_at_1():
    health = {
        "broken_job": {"consecutive_failures": 100},
    }
    collector = JobHealthCollector(runtime=_MockRuntime(health), failure_threshold=2)
    reading = await collector.collect()
    assert reading.value == 1.0


@pytest.mark.asyncio
async def test_signal_name():
    collector = JobHealthCollector()
    assert collector.signal_name == "scheduled_job_health"
