"""The runtime owns the CC failure-count sweeper (review on #4433)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from genesis.runtime.init import observability as obs_init


async def _never_ending():
    await asyncio.Event().wait()


async def test_sweeper_starts_once_with_the_bus(monkeypatch):
    import genesis.cc.invoker as inv_mod

    starts = {"n": 0}

    def fake_loop():
        starts["n"] += 1
        return _never_ending()

    monkeypatch.setattr(inv_mod, "run_failure_count_sweeper", fake_loop)
    rt = SimpleNamespace(_event_bus=object(), _failure_count_sweeper_task=None)
    obs_init._start_failure_count_sweeper(rt)
    task = rt._failure_count_sweeper_task
    assert task is not None and not task.done()
    obs_init._start_failure_count_sweeper(rt)  # re-entered init: no second loop
    assert rt._failure_count_sweeper_task is task and starts["n"] == 1
    task.cancel()


async def test_no_sweeper_without_a_bus():
    rt = SimpleNamespace(_event_bus=None, _failure_count_sweeper_task=None)
    obs_init._start_failure_count_sweeper(rt)
    assert rt._failure_count_sweeper_task is None


def test_no_sweeper_without_a_running_loop():
    rt = SimpleNamespace(_event_bus=object(), _failure_count_sweeper_task=None)
    obs_init._start_failure_count_sweeper(rt)
    assert rt._failure_count_sweeper_task is None


async def test_shutdown_cancels_the_sweeper_even_before_bootstrap():
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.__new__(GenesisRuntime)
    GenesisRuntime.__init__(rt)
    task = asyncio.create_task(_never_ending())
    rt._failure_count_sweeper_task = task
    await rt.shutdown()
    assert task.cancelled()
