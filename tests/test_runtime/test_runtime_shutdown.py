"""Tests for GenesisRuntime.ashutdown() — async-safe singleton teardown.

Item A from the post-round-3 handoff: ``reset()`` drops the singleton
reference without closing the prior instance's resources. ``ashutdown()``
is the async-aware alternative that tears the instance down cleanly
first, then clears the reference.

These tests use a real ``aiosqlite.connect(":memory:")`` connection
rather than a mock — a mocked test can't prove that a real resource
actually got closed, which is exactly what this fix is supposed to
guarantee.

One test specifically guards the ordering race that an earlier version
of ``ashutdown()`` had: if ``_instance`` is cleared *before* ``shutdown()``
awaits, a concurrent ``instance()`` call gets a fresh runtime instead
of the dying one. The current implementation clears the reference in
a ``finally`` block *after* shutdown completes; the regression test
asserts that a concurrent ``instance()`` call observes the dying
instance (not a new object) while teardown is in flight.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from genesis.runtime import GenesisRuntime


@pytest.mark.asyncio
async def test_ashutdown_closes_real_db_connection_and_clears_singleton() -> None:
    """ashutdown() on a bootstrapped-with-real-DB instance must close the
    aiosqlite connection AND clear the singleton pointer.

    Uses a real in-memory aiosqlite connection because the whole point
    of ashutdown() is to prove that real resources get closed. A mocked
    connection would pass this test trivially without exercising the
    fix.
    """
    GenesisRuntime.reset()
    rt = GenesisRuntime.instance()
    rt._db = await aiosqlite.connect(":memory:")
    rt._bootstrapped = True

    # Sanity: connection works before teardown.
    async with rt._db.execute("SELECT 1") as cursor:
        row = await cursor.fetchone()
        assert row == (1,)

    conn = rt._db
    await GenesisRuntime.ashutdown()

    # Singleton cleared.
    assert GenesisRuntime._instance is None

    # Connection closed — any further use must raise. aiosqlite raises
    # ``ValueError`` from its worker thread when the connection is
    # closed mid-operation; we accept any aiosqlite.Error or
    # ValueError/RuntimeError shape here because aiosqlite does not
    # expose a single canonical ClosedError.
    with pytest.raises((aiosqlite.Error, ValueError, RuntimeError)):
        async with conn.execute("SELECT 1") as cursor:
            await cursor.fetchone()


def test_reset_still_works_for_non_bootstrapped_instance() -> None:
    """Regression guard for the 14 sync ``reset()`` call sites.

    ``reset()`` must remain a side-effect-free no-op on a fresh
    singleton. If Item A accidentally changed its behavior, the
    autonomy / reflection / runtime test suites would break.
    """
    GenesisRuntime.reset()
    a = GenesisRuntime.instance()
    GenesisRuntime.reset()
    b = GenesisRuntime.instance()
    assert a is not b
    assert b.is_bootstrapped is False


@pytest.mark.asyncio
async def test_ashutdown_on_none_instance_is_noop() -> None:
    """ashutdown() must be safe when no singleton exists."""
    GenesisRuntime.reset()
    await GenesisRuntime.ashutdown()  # must not raise
    assert GenesisRuntime._instance is None


@pytest.mark.asyncio
async def test_ashutdown_on_non_bootstrapped_instance_clears_singleton() -> None:
    """ashutdown() on a non-bootstrapped singleton must still clear it.

    ``shutdown()`` returns early when ``_bootstrapped is False``, but
    ``ashutdown()`` must still null the class-level reference so the
    next ``instance()`` call produces a fresh object.
    """
    GenesisRuntime.reset()
    a = GenesisRuntime.instance()
    assert a.is_bootstrapped is False

    await GenesisRuntime.ashutdown()

    assert GenesisRuntime._instance is None
    b = GenesisRuntime.instance()
    assert a is not b


@pytest.mark.asyncio
async def test_ashutdown_ordering_prevents_concurrent_instance_race(
    monkeypatch,
) -> None:
    """Regression guard: an earlier ashutdown() implementation cleared
    ``_instance`` before awaiting ``shutdown()``. That opened a race
    where a concurrent ``instance()`` call during teardown got a fresh
    runtime instead of the dying one. Caught by an adversarial Codex
    review on 2026-04-10.

    The current implementation awaits ``shutdown()`` first, then clears
    ``_instance`` in a ``finally`` block. A concurrent ``instance()``
    call during teardown must therefore still return the dying
    instance — degraded but consistent with in-flight work — not a
    new object.
    """
    GenesisRuntime.reset()
    first = GenesisRuntime.instance()

    # Replace shutdown with a slow stub so we can observe the in-flight
    # window. The stub yields to the loop, giving a concurrent
    # instance() call a chance to race.
    async def slow_shutdown(self: GenesisRuntime) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(GenesisRuntime, "shutdown", slow_shutdown)

    # Kick off ashutdown and immediately yield to the loop so the
    # shutdown coroutine starts awaiting.
    task = asyncio.create_task(GenesisRuntime.ashutdown())
    await asyncio.sleep(0)

    # While ashutdown is mid-flight, a concurrent instance() call must
    # still return the dying instance, NOT a fresh one.
    during_teardown = GenesisRuntime.instance()
    assert during_teardown is first, (
        "Concurrent instance() during ashutdown() returned a different "
        "object — ashutdown cleared _instance before shutdown() completed, "
        "reintroducing the ordering race caught on 2026-04-10."
    )

    await task

    # After ashutdown completes, the reference is cleared and the next
    # instance() call produces a fresh object.
    assert GenesisRuntime._instance is None
    after_teardown = GenesisRuntime.instance()
    assert after_teardown is not first


@pytest.mark.asyncio
async def test_ashutdown_clears_instance_even_when_shutdown_raises(
    monkeypatch,
) -> None:
    """If ``shutdown()`` raises unexpectedly, ``ashutdown()`` must
    still clear the singleton reference in the ``finally`` block.

    Otherwise a partially-wired runtime leaves a dangling defunct
    pointer that defeats the purpose of the next ``reset()`` /
    ``instance()`` call. Also caught by the Codex review.
    """
    GenesisRuntime.reset()
    GenesisRuntime.instance()

    async def raising_shutdown(self: GenesisRuntime) -> None:
        raise RuntimeError("simulated shutdown failure")

    monkeypatch.setattr(GenesisRuntime, "shutdown", raising_shutdown)

    # ashutdown catches and logs the exception; it must NOT propagate,
    # and the singleton reference must still be cleared.
    await GenesisRuntime.ashutdown()

    assert GenesisRuntime._instance is None


@pytest.mark.asyncio
async def test_shutdown_stops_direct_session_runner_before_db_close() -> None:
    """Review P2 ordering guard: the direct-session runner's in-flight tasks
    are cancelled-and-awaited while the DB connection is still OPEN — that's
    the whole point (the CancelledError handler persists terminal status).
    The fake runner proves it by using the DB inside its shutdown()."""
    GenesisRuntime.reset()
    rt = GenesisRuntime.instance()
    rt._db = await aiosqlite.connect(":memory:")
    rt._bootstrapped = True

    db_was_usable: list[bool] = []

    class _FakeRunner:
        async def shutdown(self) -> int:
            try:
                async with rt._db.execute("SELECT 1") as cursor:
                    db_was_usable.append((await cursor.fetchone()) == (1,))
            except Exception:
                db_was_usable.append(False)
            return 0

    rt._direct_session_runner = _FakeRunner()
    try:
        await GenesisRuntime.ashutdown()
    finally:
        GenesisRuntime.reset()

    assert db_was_usable == [True], (
        "runner.shutdown() must run BEFORE the DB closes"
    )


@pytest.mark.asyncio
async def test_stop_outbound_senders_stops_worker() -> None:
    """stop_outbound_senders() must stop the outreach recovery worker so a late
    retry can't fire through a Telegram send client the host is about to close
    (the 2026-07-15 Sentinel-approval discard)."""
    from unittest.mock import AsyncMock, MagicMock

    GenesisRuntime.reset()
    rt = GenesisRuntime.instance()
    worker = MagicMock()
    worker.stop = AsyncMock()
    rt._outreach_recovery_worker = worker

    await rt.stop_outbound_senders()

    worker.stop.assert_awaited_once()
    GenesisRuntime.reset()


@pytest.mark.asyncio
async def test_stop_outbound_senders_noop_when_worker_absent() -> None:
    """When outreach init never ran, the attribute is unset — getattr(None)
    must make this a safe no-op, never an AttributeError."""
    GenesisRuntime.reset()
    rt = GenesisRuntime.instance()
    # Ensure the attribute is genuinely absent.
    if hasattr(rt, "_outreach_recovery_worker"):
        delattr(rt, "_outreach_recovery_worker")

    await rt.stop_outbound_senders()  # must not raise
    GenesisRuntime.reset()


@pytest.mark.asyncio
async def test_stop_outbound_senders_swallows_worker_error() -> None:
    """A failing worker.stop() must never block or crash shutdown."""
    from unittest.mock import AsyncMock, MagicMock

    GenesisRuntime.reset()
    rt = GenesisRuntime.instance()
    worker = MagicMock()
    worker.stop = AsyncMock(side_effect=RuntimeError("boom"))
    rt._outreach_recovery_worker = worker

    await rt.stop_outbound_senders()  # must not raise
    worker.stop.assert_awaited_once()
    GenesisRuntime.reset()
