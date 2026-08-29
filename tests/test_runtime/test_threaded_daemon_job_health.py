"""Threaded heartbeat daemons must PERSIST job health, not only record it in memory.

Regression coverage for a silent data-loss path found live on 2026-08-29: both
heartbeat daemons called ``rt.record_job_success()`` AFTER closing the throwaway
event loop they had created for the emit. ``persist_job_health`` returns early
when no loop is running, so the ``job_health`` ROW was never written and the
``job_health`` tool -- which reads sqlite -- reported the job as if it had never
run. Measured: neither ``outreach_heartbeat`` nor ``dashboard_heartbeat``
appeared among 89 persisted job rows while both daemons were pulsing normally.

The assertion is deliberately on the ROW, never on the call. ``record_job_success``
was ALREADY being called before the fix, so a test asserting the call passes just
as happily on the broken code -- the vacuous version of this test.

Both daemons are covered because the population is exactly two: they were the only
files in ``src/genesis/`` that both call ``record_job_success`` and use
``threading.Thread``, and the outreach daemon was modelled on the dashboard one,
inheriting the defect verbatim.
"""

from __future__ import annotations

import asyncio
import threading

import aiosqlite
import pytest

from genesis.dashboard.heartbeat import DashboardHeartbeat
from genesis.db.schema import create_all_tables
from genesis.observability.types import Severity, Subsystem
from genesis.outreach.heartbeat import OutreachHeartbeat
from genesis.runtime import GenesisRuntime


class _FakeBus:
    """Minimal event bus: records emits, and is awaitable like the real one."""

    def __init__(self) -> None:
        self.emitted: list[tuple] = []

    async def emit(self, subsystem, severity, event_type, message, **kwargs):
        self.emitted.append((subsystem, event_type, message))


def _make_runtime(db: aiosqlite.Connection, bus: _FakeBus) -> GenesisRuntime:
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._job_health = {}
    rt._db = db
    rt._job_retry_registry = None
    rt._event_bus = bus  # event_bus is a read-only property over this
    return rt


async def _drain_pending() -> None:
    """Await the tracked_task background writes so the DB is settled."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_tick_from_worker_thread(daemon, rt) -> None:
    """Drive one tick from a WORKER THREAD, as in production.

    The main loop must keep running while the worker schedules onto it, so this
    yields until the thread finishes -- bounded, so a regression that never
    completes fails the test instead of hanging the suite forever.
    """
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            daemon._tick(rt, Subsystem, Severity)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=_worker, name="tick-worker")
    thread.start()
    deadline = asyncio.get_running_loop().time() + 10
    while thread.is_alive():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("daemon tick did not complete within 10s")
        await asyncio.sleep(0.01)
    thread.join(timeout=5)
    if errors:
        raise errors[0]


@pytest.mark.parametrize(
    ("factory", "job_name", "subsystem"),
    [
        (OutreachHeartbeat, "outreach_heartbeat", Subsystem.OUTREACH),
        (DashboardHeartbeat, "dashboard_heartbeat", Subsystem.DASHBOARD),
    ],
    ids=["outreach", "dashboard"],
)
async def test_tick_persists_a_job_health_row(factory, job_name, subsystem):
    """One tick on the runtime's main loop must leave a job_health ROW behind."""
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        bus = _FakeBus()
        rt = _make_runtime(db, bus)

        daemon = factory(interval_seconds=60)
        daemon._loop = asyncio.get_running_loop()  # what start() captures

        await _run_tick_from_worker_thread(daemon, rt)
        await _drain_pending()

        # Guard the guard: the tick must actually have emitted, or a missing row
        # below would prove nothing about persistence.
        assert bus.emitted, "daemon did not emit -- test proves nothing"
        assert bus.emitted[0][0] is subsystem

        async with db.execute(
            "SELECT last_success FROM job_health WHERE job_name = ?", (job_name,)
        ) as cur:
            row = await cur.fetchone()

        assert row is not None, (
            f"{job_name} left NO job_health row -- record_job_success ran without a "
            "live event loop, so persist_job_health dropped the write silently"
        )
        assert row[0] is not None, f"{job_name} row has no last_success timestamp"


@pytest.mark.parametrize(
    ("factory", "job_name"),
    [(OutreachHeartbeat, "outreach_heartbeat"), (DashboardHeartbeat, "dashboard_heartbeat")],
    ids=["outreach", "dashboard"],
)
async def test_tick_without_a_captured_loop_still_emits(factory, job_name):
    """The no-live-loop fallback must still pulse rather than raise.

    Liveness detection reads heartbeat EVENTS, so the fallback losing only the
    job_health row is a documented degradation -- but it must never cost the
    pulse itself, which is the daemon's whole purpose.
    """
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        bus = _FakeBus()
        rt = _make_runtime(db, bus)

        daemon = factory(interval_seconds=60)
        daemon._loop = None  # start() ran outside a loop

        await _run_tick_from_worker_thread(daemon, rt)

        assert bus.emitted, f"{job_name} fallback path lost the pulse"
