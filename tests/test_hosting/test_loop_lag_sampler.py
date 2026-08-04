"""StandaloneAdapter._loop_lag_sampler — event-loop stall detector.

The sampler sleeps a fixed interval and measures how much LONGER than the
interval the wake-up actually took; that excess is time the loop could not
schedule ready callbacks (blocked in synchronous work on some task) — the
condition behind proactive-recall 503s. It WARNs when drift exceeds a
threshold (``GENESIS_LOOP_LAG_WARN_MS``, default 250).

Deterministic + install-agnostic: loop.time() and asyncio.sleep are faked so
no real wall-clock time passes and the test is not timing-dependent.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from genesis.hosting import standalone
from genesis.hosting.standalone import StandaloneAdapter

pytestmark = pytest.mark.asyncio


def _adapter_with_fake_clock(monkeypatch, *, times, stop_after=1):
    """Wire an adapter whose sampler runs ``stop_after`` iterations then stops.

    ``times`` supplies the loop.time() readings — two per iteration (t0 before
    sleep, then the post-sleep read). asyncio.sleep is faked to set the shutdown
    event after ``stop_after`` calls so the while-loop exits, and to advance
    nothing real.
    """
    import asyncio as _asyncio

    adapter = StandaloneAdapter()
    adapter._shutdown_event = _asyncio.Event()

    clock = iter(times)
    fake_loop = MagicMock()
    fake_loop.time = MagicMock(side_effect=lambda: next(clock))
    monkeypatch.setattr(standalone.asyncio, "get_running_loop", lambda: fake_loop)

    sleeps = {"n": 0}

    async def _fake_sleep(_interval):
        sleeps["n"] += 1
        if sleeps["n"] >= stop_after:
            adapter._shutdown_event.set()

    monkeypatch.setattr(standalone.asyncio, "sleep", _fake_sleep)
    return adapter


async def test_warns_when_drift_exceeds_threshold(monkeypatch, caplog):
    # interval 0.5s, post-sleep clock 0.95s → drift 450ms > 250ms default.
    adapter = _adapter_with_fake_clock(monkeypatch, times=[0.0, 0.95])
    with caplog.at_level(logging.WARNING):
        await adapter._loop_lag_sampler()
    lag_warns = [r.getMessage() for r in caplog.records if "event-loop lag" in r.getMessage()]
    assert lag_warns, "a >threshold stall must WARN"
    assert "450ms" in lag_warns[0]


async def test_silent_when_drift_below_threshold(monkeypatch, caplog):
    # drift 50ms < 250ms → no warning.
    adapter = _adapter_with_fake_clock(monkeypatch, times=[0.0, 0.55])
    with caplog.at_level(logging.WARNING):
        await adapter._loop_lag_sampler()
    assert not [r for r in caplog.records if "event-loop lag" in r.getMessage()]


async def test_env_threshold_raises_the_bar(monkeypatch, caplog):
    # A 450ms drift that WOULD warn at the default stays silent when the env
    # lever raises the threshold above it (no restart of the logic required).
    monkeypatch.setenv("GENESIS_LOOP_LAG_WARN_MS", "1000")
    adapter = _adapter_with_fake_clock(monkeypatch, times=[0.0, 0.95])
    with caplog.at_level(logging.WARNING):
        await adapter._loop_lag_sampler()
    assert not [r for r in caplog.records if "event-loop lag" in r.getMessage()]


async def test_bad_env_threshold_falls_back_to_default(monkeypatch, caplog):
    # A non-numeric override must not crash the sampler; it falls back to 250ms.
    monkeypatch.setenv("GENESIS_LOOP_LAG_WARN_MS", "not-a-number")
    adapter = _adapter_with_fake_clock(monkeypatch, times=[0.0, 0.95])
    with caplog.at_level(logging.WARNING):
        await adapter._loop_lag_sampler()
    assert [r for r in caplog.records if "event-loop lag" in r.getMessage()]


async def test_sustained_lag_warns_once_then_reports_clear(monkeypatch, caplog):
    """A multi-sample stall must WARN exactly once (episode debounce), then emit
    an INFO with the peak drift when it clears — not one line per sample."""
    # iter1: 0.0→0.95 drift 450 (enter, WARN); iter2: 0.95→2.05 drift 600 (peak,
    # suppressed); iter3: 2.05→2.55 drift 0 (clear, INFO with peak 600).
    adapter = _adapter_with_fake_clock(
        monkeypatch,
        times=[0.0, 0.95, 0.95, 2.05, 2.05, 2.55],
        stop_after=3,
    )
    with caplog.at_level(logging.INFO):
        await adapter._loop_lag_sampler()
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    clears = [r.getMessage() for r in caplog.records if "event-loop lag cleared" in r.getMessage()]
    assert len(warns) == 1, f"sustained stall must warn once, got {warns}"
    assert "suppressed until it clears" in warns[0]
    assert len(clears) == 1
    assert "peak 600ms" in clears[0]
