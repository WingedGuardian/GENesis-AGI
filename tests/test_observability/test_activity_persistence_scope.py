"""Scoped admission affects automatic writes, while retaining process counters."""

import asyncio

import pytest

from genesis.observability import provider_activity as activity


@pytest.fixture
def tracker(monkeypatch):
    tracker = activity.ProviderActivityTracker()
    tracker.set_db(object())
    # Inspect real producer admission independently of asynchronous batching.
    monkeypatch.setattr(tracker, "_schedule_flush", lambda: None)
    return tracker


def record(tracker, name):
    tracker.record(name, latency_ms=1, success=True)


async def test_nested_scope_keeps_counters_and_restores_write_admission(tracker):
    record(tracker, "ordinary-before")
    with activity.suppress_activity_persistence():
        record(tracker, "validator-outer")
        with activity.suppress_activity_persistence():
            record(tracker, "validator-inner")
        record(tracker, "validator-outer-again")
    record(tracker, "ordinary-after")
    assert [row[0] for row in tracker._write_batch] == ["ordinary-before", "ordinary-after"]
    assert sum(row["calls"] for row in tracker.summary()) == 5
    assert tracker.persist_activity is True


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failed_scope_restores_write_admission(tracker, error):
    with pytest.raises(error), activity.suppress_activity_persistence():
        record(tracker, "validator")
        raise error()
    record(tracker, "ordinary")
    assert [row[0] for row in tracker._write_batch] == ["ordinary"]
    assert tracker.summary("validator")["calls"] == 1


async def test_concurrent_and_detached_context_does_not_toggle_tracker(tracker):
    barrier = asyncio.Barrier(2)
    release_child = asyncio.Event()
    children = []

    async def child():
        await release_child.wait()
        record(tracker, "validator-child-after-parent-exit")

    async def validator():
        with activity.suppress_activity_persistence():
            record(tracker, "validator-before")
            children.append(asyncio.create_task(child()))
            await barrier.wait()
            assert tracker.persist_activity is True
            record(tracker, "validator-after")

    async def ordinary():
        record(tracker, "ordinary-before")
        await barrier.wait()
        assert tracker.persist_activity is True
        record(tracker, "ordinary-after")

    await asyncio.gather(validator(), ordinary())
    release_child.set()
    await asyncio.gather(*children)
    assert [row[0] for row in tracker._write_batch] == ["ordinary-before", "ordinary-after"]
    assert sum(row["calls"] for row in tracker.summary()) == 5


async def test_construction_off_and_separate_trackers_remain_independent(tracker, monkeypatch):
    disabled = activity.ProviderActivityTracker()
    disabled.persist_activity = False
    disabled.set_db(object())
    monkeypatch.setattr(disabled, "_schedule_flush", lambda: None)
    with activity.suppress_activity_persistence():
        record(tracker, "suppressed-enabled")
        record(disabled, "suppressed-disabled")
    record(tracker, "ordinary-enabled")
    record(disabled, "ordinary-disabled")
    assert [row[0] for row in tracker._write_batch] == ["ordinary-enabled"]
    assert disabled._write_batch == []
    assert sum(row["calls"] for row in disabled.summary()) == 2
    assert disabled.persist_activity is False
