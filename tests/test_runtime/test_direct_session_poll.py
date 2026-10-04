"""Tests for the direct-session poll loop's periodic stale-claim recovery.

idx 42: ``recover_stale_claims`` ran only once at startup; the poll loop never
re-ran it, so a claim younger than the 120s floor at a fast restart stayed
``claimed`` forever. The loop must re-run recovery periodically (~60s).
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

from genesis.db.crud import direct_session_queue as dsq
from genesis.runtime.init import direct_session as ds_init


class _FakeRunner:
    """Saturated runner: forces the poll loop to skip claiming, isolating the
    recovery path (recovery runs BEFORE the capacity check)."""

    _MAX_CONCURRENT = 1

    def active_count(self) -> int:
        return 99


@pytest.mark.asyncio
async def test_poll_loop_reruns_recovery_periodically(db, monkeypatch):
    call_count = {"n": 0}
    real = dsq.recover_stale_claims

    async def _counting(conn, max_age_s: int = 120):
        call_count["n"] += 1
        return await real(conn, max_age_s=max_age_s)

    monkeypatch.setattr("genesis.db.crud.direct_session_queue.recover_stale_claims", _counting)
    # Make the loop spin fast and recover every iteration.
    monkeypatch.setattr(ds_init, "_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(ds_init, "_RECOVERY_EVERY_N_POLLS", 1)

    task = asyncio.create_task(ds_init._direct_session_poll(_FakeRunner(), db))
    await asyncio.sleep(0.05)  # allow the startup call + several loop iterations
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # 1 startup call + >=1 in-loop call. The bug (recovery only at startup)
    # would leave this at exactly 1.
    assert call_count["n"] >= 2


@pytest.mark.asyncio
async def test_poll_loop_leaves_queue_pending_during_deploy(db, monkeypatch):
    qid = await dsq.enqueue(db, prompt="wait for deploy")
    monkeypatch.setattr(ds_init, "_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr("genesis.env.update_in_progress", lambda: True)
    claim_next = AsyncMock(wraps=dsq.claim_next)
    monkeypatch.setattr(dsq, "claim_next", claim_next)

    runner = _FakeRunner()
    runner.active_count = lambda: 0
    task = asyncio.create_task(ds_init._direct_session_poll(runner, db))
    await asyncio.sleep(0.02)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    claim_next.assert_not_awaited()
    row = await dsq.get_by_id(db, qid)
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_poll_loop_claims_queue_when_no_deploy(db, monkeypatch):
    qid = await dsq.enqueue(db, prompt="dispatch")
    monkeypatch.setattr(ds_init, "_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr("genesis.env.update_in_progress", lambda: False)
    spawned = asyncio.Event()

    runner = _FakeRunner()
    runner.active_count = lambda: 0

    async def spawn(request):
        spawned.set()
        return "session-1"

    runner.spawn = spawn
    task = asyncio.create_task(ds_init._direct_session_poll(runner, db))
    await asyncio.wait_for(spawned.wait(), timeout=1)
    await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    row = await dsq.get_by_id(db, qid)
    assert row["status"] == "dispatched"


@pytest.mark.asyncio
async def test_poll_loop_recovery_resets_stale_claim(db, monkeypatch):
    """A stale claim left after the startup pass is recovered by the loop."""
    monkeypatch.setattr(ds_init, "_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(ds_init, "_RECOVERY_EVERY_N_POLLS", 1)

    qid = await dsq.enqueue(db, prompt="stuck")
    await dsq.claim_next(db)
    # Backdate the claim well past the 120s floor.
    await db.execute(
        "UPDATE direct_session_queue SET claimed_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", qid),
    )
    await db.commit()

    task = asyncio.create_task(ds_init._direct_session_poll(_FakeRunner(), db))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    row = await dsq.get_by_id(db, qid)
    assert row["status"] == "pending"
    assert row["claimed_at"] is None
