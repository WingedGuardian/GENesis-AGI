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


@pytest.mark.asyncio
async def test_firing_note_names_the_failing_jobs():
    # When jobs are failing, the baseline_note (the field rendered into reflection
    # prompts) must NAME which jobs — the healthy generic note lacks any job name, so
    # asserting the name proves the fix injected it. metadata is never rendered, so
    # names must live in baseline_note, not metadata (matching the docstring's intent).
    health = {
        "weekly_assessment": {"consecutive_failures": 3},
        "surplus_tick": {"consecutive_failures": 0},
    }
    reading = await JobHealthCollector(runtime=_MockRuntime(health), failure_threshold=2).collect()
    assert reading.value == 0.75
    note = (reading.baseline_note or "").lower()
    assert "weekly_assessment" in note, note
    assert "surplus_tick" not in note, note  # healthy job not named
    assert "metadata" not in note, note


@pytest.mark.asyncio
async def test_firing_note_names_all_failing_jobs():
    health = {
        "job_alpha": {"consecutive_failures": 5},
        "job_beta": {"consecutive_failures": 4},
    }
    reading = await JobHealthCollector(runtime=_MockRuntime(health), failure_threshold=2).collect()
    note = (reading.baseline_note or "").lower()
    assert "job_alpha" in note and "job_beta" in note, note


@pytest.mark.asyncio
async def test_healthy_note_has_no_job_names():
    health = {"weekly_assessment": {"consecutive_failures": 0}}
    reading = await JobHealthCollector(runtime=_MockRuntime(health), failure_threshold=2).collect()
    assert reading.value == 0.0
    assert "weekly_assessment" not in (reading.baseline_note or "").lower()
