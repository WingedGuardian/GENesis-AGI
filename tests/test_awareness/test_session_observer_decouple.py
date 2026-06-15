"""Tests for decoupling the session observer from the awareness tick.

The session observer used to be awaited INSIDE the tick lock; under provider
exhaustion its LLM call held the lock 16+ min and starved the heartbeat. It now
runs out-of-band via tracked_task with an asyncio.Lock single-flight guard.
"""
from __future__ import annotations

import asyncio

import pytest

from genesis.awareness.loop import AwarenessLoop


@pytest.mark.asyncio
async def test_on_tick_dispatches_observer_out_of_band(db, monkeypatch):
    """_on_tick must hand the observer to tracked_task, NOT await it inline."""
    loop = AwarenessLoop(db=db, collectors=[])

    observer_awaited = {"v": False}

    async def _observer():
        observer_awaited["v"] = True

    loop.set_session_observer(_observer)

    dispatched_names: list[str] = []

    def _fake_tracked_task(coro, *, name="", **kw):
        dispatched_names.append(name)
        coro.close()  # record the dispatch without running it
        return None

    monkeypatch.setattr("genesis.util.tasks.tracked_task", _fake_tracked_task)

    await loop._on_tick()

    # The observer was dispatched as an out-of-band task, never awaited in-tick.
    assert any(n.startswith("session-observer-") for n in dispatched_names)
    assert observer_awaited["v"] is False


@pytest.mark.asyncio
async def test_run_session_observer_single_flight(db):
    """A second run is skipped while a prior one still holds the lock."""
    loop = AwarenessLoop(db=db, collectors=[])

    calls = {"n": 0}
    release = asyncio.Event()

    async def _slow_observer():
        calls["n"] += 1
        await release.wait()  # hold the lock until released
        return None

    loop.set_session_observer(_slow_observer)

    first = asyncio.create_task(loop._run_session_observer())
    await asyncio.sleep(0.05)  # let `first` acquire the lock and start

    # Second invocation while the lock is held → skipped (no second call).
    await loop._run_session_observer()
    assert calls["n"] == 1

    release.set()
    await first
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_run_session_observer_swallows_errors(db):
    """A failing observer must not raise out of the out-of-band runner."""
    loop = AwarenessLoop(db=db, collectors=[])

    async def _boom():
        raise RuntimeError("observer blew up")

    loop.set_session_observer(_boom)
    await loop._run_session_observer()  # must not raise
    # lock released even after failure → a subsequent run can proceed
    assert not loop._session_observer_lock.locked()
