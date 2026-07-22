"""Wire the rate-limit resume engine onto the learning scheduler.

CronTrigger (not IntervalTrigger): IntervalTrigger resets on server restart, so
a frequently-restarting server would never fire it. Every 10 minutes, gated per
tick by ``cc_rate_limit_resume`` mode (off/propose_only/live).
"""

from __future__ import annotations

from genesis.cc.rate_limit_resume import run_resume_tick


def _wire_rate_limit_resume(scheduler, rt) -> None:
    from apscheduler.triggers.cron import CronTrigger

    async def _tick() -> None:
        await run_resume_tick(rt)

    scheduler.add_job(
        _tick,
        CronTrigger(minute="*/10"),
        id="rate_limit_resume",
        max_instances=1,
        misfire_grace_time=600,
    )
