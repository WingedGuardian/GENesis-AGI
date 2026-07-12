"""Registration coverage for the genesis.db drip-table retention jobs (D3).

Locks that ``_wire_drip_retention_jobs`` actually registers all three prune jobs — if an
``add_job`` call were dropped or a job id changed, this fails (the crud prunes themselves
are covered separately). Uses a real AsyncIOScheduler + a stub runtime; no full runtime init.
"""

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from genesis.runtime.init.learning import _wire_drip_retention_jobs

pytestmark = pytest.mark.asyncio

_EXPECTED = ("execution_traces_prune", "cost_events_prune", "file_modifications_prune")


class _StubRT:
    _db = None

    def record_job_success(self, *_a):
        pass

    def record_job_failure(self, *_a):
        pass


async def test_wire_drip_retention_jobs_registers_all_three():
    sched = AsyncIOScheduler()
    _wire_drip_retention_jobs(sched, _StubRT())
    sched.start(paused=True)  # flush pending jobs into the jobstore without firing them
    try:
        for job_id in _EXPECTED:
            job = sched.get_job(job_id)
            assert job is not None, f"{job_id} not registered"
            assert isinstance(job.trigger, CronTrigger)
    finally:
        sched.shutdown(wait=False)


async def test_wire_git_health_deep_job_registered_as_cron():
    """F.1: the daily git fsck deep check must register as a restart-safe
    CronTrigger (an IntervalTrigger would reset each restart and never fire on a
    frequently-restarted box)."""
    from apscheduler.triggers.interval import IntervalTrigger

    from genesis.runtime.init.learning import _wire_git_health_deep_job

    sched = AsyncIOScheduler()
    _wire_git_health_deep_job(sched, _StubRT())
    sched.start(paused=True)
    try:
        job = sched.get_job("git_health_deep")
        assert job is not None, "git_health_deep not registered"
        assert isinstance(job.trigger, CronTrigger)
        assert not isinstance(job.trigger, IntervalTrigger)
    finally:
        sched.shutdown(wait=False)
