"""Tests for the decoupled status-writer loop.

The status-writer loop runs independently of the awareness tick so a slow
tick (e.g. a long Light reflection) cannot delay the status.json refresh
and trip the watchdog into a false restart.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.runtime.init.memory import run_status_writer_loop


def _fake_runtime(writer) -> SimpleNamespace:
    rt = SimpleNamespace()
    rt._status_writer = writer
    rt.record_job_success = MagicMock()
    rt.record_job_failure = MagicMock()
    return rt


async def _run_for(coro, iterations: int, interval_s: float) -> asyncio.Task:
    task = asyncio.create_task(coro)
    await asyncio.sleep(interval_s * (iterations + 0.5))
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return task


@pytest.mark.asyncio
async def test_loop_calls_writer_each_interval():
    """Loop wakes every interval and calls writer.write() once per cycle."""
    writer = AsyncMock()
    rt = _fake_runtime(writer)

    await _run_for(run_status_writer_loop(rt, interval_s=0.02), iterations=3, interval_s=0.02)

    assert writer.write.await_count >= 2
    assert rt.record_job_success.call_count >= 2
    rt.record_job_failure.assert_not_called()


@pytest.mark.asyncio
async def test_loop_survives_transient_write_failure():
    """A single write() exception does not terminate the loop."""
    writer = AsyncMock()
    call_count = {"n": 0}

    async def flaky_write() -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("disk full (transient)")

    writer.write.side_effect = flaky_write
    rt = _fake_runtime(writer)

    await _run_for(run_status_writer_loop(rt, interval_s=0.02), iterations=3, interval_s=0.02)

    assert writer.write.await_count >= 3
    assert rt.record_job_failure.call_count >= 1
    assert rt.record_job_success.call_count >= 1


@pytest.mark.asyncio
async def test_loop_cancellation_is_clean():
    """Cancelling mid-sleep exits cleanly without re-raising."""
    writer = AsyncMock()
    rt = _fake_runtime(writer)

    task = asyncio.create_task(run_status_writer_loop(rt, interval_s=10.0))
    await asyncio.sleep(0.01)  # let the task enter the sleep
    task.cancel()
    # Should not raise CancelledError — loop handles it.
    await task
    writer.write.assert_not_called()


@pytest.mark.asyncio
async def test_loop_skips_when_writer_is_none():
    """If writer is hot-swapped to None, loop skips without crashing."""
    rt = _fake_runtime(writer=None)

    await _run_for(run_status_writer_loop(rt, interval_s=0.02), iterations=2, interval_s=0.02)

    # No success recorded — writer was None every cycle.
    rt.record_job_success.assert_not_called()
    rt.record_job_failure.assert_not_called()
