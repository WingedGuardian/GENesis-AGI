"""WS-D — health snapshot: offload blocking I/O, gather failure isolation, single-flight.

Three behaviours:
1. F4 — the subprocess-heavy `collect_service_status()` runs OFF the event-loop
   thread (via `services_async()`), while the sentinel assembly stays on-loop.
2. F10 — one failing sub-snapshot is isolated to a `{"status": "error"}` section
   instead of taking down the whole snapshot.
3. Coalescer — concurrent `snapshot()` callers share ONE in-flight computation.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from genesis.observability.health_data import HealthDataService


def _mock_db():
    """Fake aiosqlite connection whose queries return empty result sets."""
    db = AsyncMock()

    async def _execute(sql, params=None):
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)
        cursor.fetchall = AsyncMock(return_value=[])
        return cursor

    db.execute = AsyncMock(side_effect=_execute)
    return db


# ---------------------------------------------------------------------------
# F4 — offload the subprocess-heavy collection, keep sentinel read on the loop
# ---------------------------------------------------------------------------

def test_services_async_offloads_collection_to_worker_thread(monkeypatch):
    """`collect_service_status()` must run in a worker thread, not on the loop."""
    from genesis.observability.snapshots.services import services_async

    loop_thread = threading.get_ident()
    recorded: dict[str, int] = {}

    def fake_collect():
        recorded["thread"] = threading.get_ident()
        return {"bridge": {"active_state": "active"}}

    # _collect_service_status_safe imports collect_service_status at call time.
    monkeypatch.setattr(
        "genesis.observability.service_status.collect_service_status", fake_collect
    )

    result = asyncio.run(services_async())

    assert recorded.get("thread") is not None, "collect_service_status was never called"
    assert recorded["thread"] != loop_thread, "collection ran on the event-loop thread"
    # The on-loop assembly (host framework + sentinel) must still be present.
    assert "host_framework" in result
    assert "sentinel" in result


def test_services_sync_and_async_agree_on_shape(monkeypatch):
    """The sync `services()` and async `services_async()` produce the same keys."""
    from genesis.observability.snapshots.services import services, services_async

    def fake_collect():
        return {"bridge": {"active_state": "active"}}

    monkeypatch.setattr(
        "genesis.observability.service_status.collect_service_status", fake_collect
    )

    sync_result = services()
    async_result = asyncio.run(services_async())
    assert set(sync_result) == set(async_result)


# ---------------------------------------------------------------------------
# F10 — a failing sub-snapshot is isolated, not fatal
# ---------------------------------------------------------------------------

def test_failing_section_is_isolated_not_fatal(monkeypatch):
    """One raising sub-snapshot degrades that section; the rest survive."""
    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    # call_sites is imported inside _compute_snapshot from the snapshots package,
    # so patch it on that package namespace.
    monkeypatch.setattr("genesis.observability.snapshots.call_sites", boom)

    svc = HealthDataService(db=_mock_db())
    snap = asyncio.run(svc.snapshot())

    assert snap["call_sites"]["status"] == "error"
    assert "boom" in snap["call_sites"]["error"]
    # Every other section is still present and did not inherit the failure.
    for key in ("cc_sessions", "queues", "cost", "services", "conversation", "proactive_memory"):
        assert key in snap, f"section {key} missing after an isolated failure"
    assert snap["cc_sessions"].get("status") != "error"


# ---------------------------------------------------------------------------
# Coalescer — concurrent callers share one in-flight computation
# ---------------------------------------------------------------------------

def test_concurrent_snapshots_coalesce_to_single_compute():
    """Two overlapping snapshot() calls trigger _compute_snapshot exactly once."""
    svc = HealthDataService(db=_mock_db())

    call_count = 0

    async def counting_compute():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # hold the in-flight window open for the 2nd caller
        return {"marker": call_count}

    svc._compute_snapshot = counting_compute

    async def _run():
        return await asyncio.gather(svc.snapshot(), svc.snapshot())

    r1, r2 = asyncio.run(_run())

    assert call_count == 1, f"expected a single coalesced compute, got {call_count}"
    assert r1 is r2, "coalesced callers should receive the same shared result object"


def test_completed_snapshot_is_not_reused():
    """A completed in-flight handle must not coalesce — sequential calls recompute."""
    svc = HealthDataService(db=_mock_db())

    calls = 0

    async def counting_compute():
        nonlocal calls
        calls += 1
        return {"n": calls}

    svc._compute_snapshot = counting_compute

    asyncio.run(svc.snapshot())
    asyncio.run(svc.snapshot())

    # Sequential (non-overlapping) calls must each recompute — no stale coalescing.
    assert calls == 2
    # The handle is never left pointing at a live task (cleared by done-callback;
    # the .done() guard makes a lingering completed handle harmless regardless).
    assert svc._inflight is None or svc._inflight.done()


def test_caller_cancellation_does_not_abort_coalesced_siblings():
    """One caller's cancellation must NOT cancel the shared compute for siblings.

    Regression for the unshielded-task bug: the dashboard route's 15s timeout
    cancels its snapshot() future; without asyncio.shield that would cancel the
    in-flight computation for the sentinel/ego callers coalescing on it.
    """
    svc = HealthDataService(db=_mock_db())
    started = asyncio.Event()
    release = asyncio.Event()
    completed = False

    async def slow_compute():
        nonlocal completed
        started.set()
        await release.wait()  # held in-flight until we allow completion
        completed = True
        return {"ok": True}

    svc._compute_snapshot = slow_compute

    async def _run():
        a = asyncio.create_task(svc.snapshot())   # owner
        await started.wait()                       # compute is now in-flight
        b = asyncio.create_task(svc.snapshot())    # coalesces onto the same task
        await asyncio.sleep(0)                      # let b reach its await
        a.cancel()                                 # simulate the dashboard 15s timeout
        with pytest.raises(asyncio.CancelledError):
            await a
        release.set()                              # allow the shared compute to finish
        return await b                             # sibling must still get the result

    result_b = asyncio.run(_run())
    assert completed is True, "shared compute was cancelled by the other caller"
    assert result_b == {"ok": True}, "coalesced sibling did not receive the result"
