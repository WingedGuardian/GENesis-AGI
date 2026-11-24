"""The reconcile sweep's pair discovery runs in a fresh spawn process (#4877).

Pins: the child returns exactly what a direct call returns (cap cut-off
included) and leaves the caller's set alone; a crash, a timeout and a
cancellation each raise and leave no worker process behind; a failed sweep
leaves the cursor where it was, so the slice retries.

The crash and hang are made REAL in the child: an argument whose unpickling
calls ``os._exit`` or ``time.sleep`` runs inside the worker before
``_compute_sweep_pairs`` starts, which a parent-side patch could not do.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from concurrent.futures.process import BrokenProcessPool

import pytest

from genesis.db.crud import ego as ego_crud
from genesis.memory import entity_adjudication as adj


class _DieInChild:
    def __reduce__(self):
        return (os._exit, (3,))


class _HangInChild:
    def __reduce__(self):
        return (time.sleep, (120,))


def _names() -> tuple[list, dict]:
    cluster = [
        ("neural monitor", "e1", "concept"),
        ("neural-monitor", "e2", "concept"),
        ("neural_monitor", "e3", "concept"),
        ("dream cycle", "e4", "concept"),
        ("dream cycle runner", "e5", "concept"),
        ("unrelated thing", "e6", "concept"),
    ]
    return cluster, {"cluster": cluster, "person": [], "org": []}


async def _no_workers_left(before: set[int], wait_s: float = 10.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if {p.pid for p in multiprocessing.active_children()} <= before:
            return True
        await asyncio.sleep(0.1)
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [100, 2])
async def test_child_returns_the_direct_result_and_leaves_seen_alone(cap):
    slice_entities, groups = _names()
    seen = {"e1|e3"}
    direct = adj._compute_sweep_pairs(slice_entities, groups, set(seen), cap)
    assert direct  # the fixture really produces pairs
    before = {p.pid for p in multiprocessing.active_children()}
    got = await adj._sweep_pairs_in_child(slice_entities, groups, seen, cap)
    assert got == direct
    assert len(got) <= cap
    assert seen == {"e1|e3"}
    assert await _no_workers_left(before)


@pytest.mark.asyncio
async def test_a_crashed_child_raises_and_leaves_no_worker():
    _, groups = _names()
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(BrokenProcessPool):
        await adj._sweep_pairs_in_child([_DieInChild()], groups, set(), 10)
    assert await _no_workers_left(before)


@pytest.mark.asyncio
async def test_a_hung_child_times_out_and_is_killed(monkeypatch):
    monkeypatch.setattr(adj, "_SWEEP_TIMEOUT_S", 1.0)
    _, groups = _names()
    before = {p.pid for p in multiprocessing.active_children()}
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        await adj._sweep_pairs_in_child([_HangInChild()], groups, set(), 10)
    assert await _no_workers_left(before)
    assert time.monotonic() - t0 < 60  # killed, not waited out


@pytest.mark.asyncio
async def test_cancellation_kills_the_child():
    _, groups = _names()
    before = {p.pid for p in multiprocessing.active_children()}
    task = asyncio.create_task(adj._sweep_pairs_in_child([_HangInChild()], groups, set(), 10))
    await asyncio.sleep(2.0)  # the worker is up and sleeping
    task.cancel()
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 5
    assert await _no_workers_left(before)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [BrokenProcessPool("worker died"), TimeoutError()])
async def test_a_failed_sweep_leaves_the_cursor(db, monkeypatch, exc):
    await ego_crud.set_state(db, key=adj._SWEEP_CURSOR_KEY, value=json.dumps({"offset": 400}))

    async def _fail(*_args):
        raise exc

    monkeypatch.setattr(adj, "_sweep_pairs_in_child", _fail)
    with pytest.raises(type(exc)):
        await adj.maybe_run_sweep(db, drain_budget=50, slice_size=200, enqueue_cap=50)
    state = json.loads(await ego_crud.get_state(db, adj._SWEEP_CURSOR_KEY))
    assert state["offset"] == 400
