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

_EXPECTED = (
    "execution_traces_prune",
    "cost_events_prune",
    "file_modifications_prune",
    "job_run_events_prune",
    "alert_events_prune",
    "deferred_work_prune",
    "graduation_events_prune",
    "voice_hygiene",
)


class _StubRT:
    _db = None

    def record_job_success(self, *_a):
        pass

    def record_job_failure(self, *_a):
        pass


async def test_wire_drip_retention_jobs_registers_all():
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


# NOTE: the daily deep `git fsck --full` (F.1) is NO LONGER a learning-scheduler
# job — it runs from the awareness loop (`_check_git_health_deep`) so it survives a
# router-degraded startup that skips learning init. Its coverage lives in
# tests/test_awareness/test_git_health_check.py.
