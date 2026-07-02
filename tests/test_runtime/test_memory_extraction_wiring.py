"""The memory-extraction job must fire shortly after boot, not boot+interval.

``IntervalTrigger``'s first run is boot+interval and RESETS on every restart,
so a server that restarts more often than the 2h interval would starve memory
extraction entirely (the IntervalTrigger-resets-on-restart trap flagged in
CLAUDE.md). Pinning an explicit ``next_run_time`` ~60s out guarantees a run
every boot while preserving the steady interval. These tests lock that in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from genesis.runtime.init.surplus import _wire_memory_extraction_job


async def _noop() -> None:
    return None


class TestMemoryExtractionBootRun:
    async def test_registered_with_boot_run_not_two_hours(self):
        sched = AsyncIOScheduler()
        sched.start(paused=True)  # compute next_run_time without firing
        try:
            now = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
            _wire_memory_extraction_job(
                sched, coro=_noop, extraction_hours=2, now=now,
            )
            job = sched.get_job("memory_extraction")
            assert job is not None
            delta = job.next_run_time - now
            # Boot-run: first fire ~60s after wiring, NOT 2h later.
            assert timedelta(seconds=30) <= delta <= timedelta(seconds=90), (
                f"expected ~60s boot-run, got {delta}"
            )
        finally:
            sched.shutdown(wait=False)

    async def test_uses_cron_cadence_not_interval(self):
        """Restart-safe: CronTrigger (wall-clock), never a resets-on-restart
        IntervalTrigger — the >1h convention this fix exists to honor."""
        sched = AsyncIOScheduler()
        sched.start(paused=True)
        try:
            _wire_memory_extraction_job(sched, coro=_noop, extraction_hours=2)
            job = sched.get_job("memory_extraction")
            assert job is not None
            assert isinstance(job.trigger, CronTrigger)
            hour_field = next(f for f in job.trigger.fields if f.name == "hour")
            assert "*/2" in str(hour_field)
        finally:
            sched.shutdown(wait=False)

    async def test_job_id_stable(self):
        sched = AsyncIOScheduler()
        sched.start(paused=True)
        try:
            _wire_memory_extraction_job(sched, coro=_noop, extraction_hours=2)
            assert sched.get_job("memory_extraction") is not None
        finally:
            sched.shutdown(wait=False)
