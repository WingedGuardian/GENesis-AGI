"""User job scheduler — APScheduler-backed cron jobs that dispatch CC sessions.

Users create jobs via MCP tools. Each job has a cron expression and a
dispatch prompt. When the cron fires, a background DirectSession runs
the prompt with the configured profile/model/effort.

The scheduler loads all active jobs from the DB on start and registers
them as APScheduler CronTrigger jobs. Jobs can be added, paused,
resumed, and run immediately at runtime.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.observability.event_bus import GenesisEventBus

logger = logging.getLogger(__name__)


class UserJobScheduler:
    """Owns an APScheduler instance for user-defined cron jobs."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._scheduler = None  # AsyncIOScheduler, created in start()

    @property
    def is_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    async def start(self) -> None:
        """Load active jobs from DB and start the APScheduler."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler()

        from genesis.db.crud import user_jobs as crud

        jobs = await crud.list_jobs(self._db, status="active")
        for job in jobs:
            self._register_job(job)

        self._scheduler.start()
        logger.info("User job scheduler started with %d active job(s)", len(jobs))

    async def stop(self) -> None:
        """Shut down the APScheduler."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("User job scheduler stopped")

    def _register_job(self, job: dict) -> bool:
        """Register a single job with APScheduler. Returns True on success."""
        if not self._scheduler:
            return False

        from zoneinfo import ZoneInfo

        from apscheduler.triggers.cron import CronTrigger

        from genesis.env import user_timezone

        cron_expr = job.get("cron_expression", "")
        job_id = job.get("id", "")
        try:
            tz = ZoneInfo(user_timezone())
            trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)
            self._scheduler.add_job(
                self._dispatch_job,
                trigger,
                id=f"user_job:{job_id}",
                args=(job_id,),
                max_instances=1,
                misfire_grace_time=300,
                replace_existing=True,
            )
            logger.info(
                "Registered user job %s '%s' (cron=%s)",
                job_id[:8], job.get("title", ""), cron_expr,
            )
            return True
        except Exception:
            logger.exception("Failed to register user job %s", job_id[:8])
            return False

    def _unregister_job(self, job_id: str) -> None:
        """Remove a job from APScheduler."""
        if not self._scheduler:
            return
        ap_id = f"user_job:{job_id}"
        with contextlib.suppress(Exception):
            self._scheduler.remove_job(ap_id)

    async def add_job(self, job_id: str) -> bool:
        """Register a newly created job with APScheduler."""
        from genesis.db.crud import user_jobs as crud

        job = await crud.get_job(self._db, job_id)
        if not job:
            return False
        return self._register_job(job)

    async def remove_job(self, job_id: str) -> bool:
        """Remove and delete a job."""
        self._unregister_job(job_id)
        from genesis.db.crud import user_jobs as crud

        return await crud.delete_job(self._db, job_id)

    async def pause_job(self, job_id: str) -> bool:
        """Pause a job (remove from scheduler, set status=paused)."""
        self._unregister_job(job_id)
        from genesis.db.crud import user_jobs as crud

        return await crud.update_job(self._db, job_id, status="paused")

    async def resume_job(self, job_id: str) -> bool:
        """Resume a paused job."""
        from genesis.db.crud import user_jobs as crud

        updated = await crud.update_job(self._db, job_id, status="active")
        if updated:
            job = await crud.get_job(self._db, job_id)
            if job:
                self._register_job(job)
        return updated

    async def run_now(self, job_id: str) -> str | None:
        """Immediately dispatch a job, bypassing the cron schedule.

        Returns the session ID if dispatched, or None on failure.
        """
        return await self._dispatch_job(job_id)

    async def _dispatch_job(self, job_id: str) -> str | None:
        """Fire a user job by dispatching a DirectSessionRequest."""
        from genesis.db.crud import user_jobs as crud

        job = await crud.get_job(self._db, job_id)
        if not job:
            logger.error("User job %s not found for dispatch", job_id[:8])
            return None

        run_id: str | None = None
        try:
            from genesis.cc.direct_session import DirectSessionRequest
            from genesis.cc.types import CCModel, EffortLevel
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            runner = getattr(rt, "_direct_session_runner", None)
            if runner is None:
                logger.error("DirectSessionRunner not available for user job %s", job_id[:8])
                await crud.update_job(self._db, job_id, last_status="failed")
                return None

            request = DirectSessionRequest(
                prompt=job["dispatch_prompt"],
                profile=job.get("profile", "observe"),
                model=CCModel(job.get("model", "sonnet")),
                effort=EffortLevel(job.get("effort", "medium")),
                notify=True,
                source_tag=f"user_job:{job_id}",
                caller_context=f"user_job:{job_id}",
            )

            # Record the run start
            run_id = await crud.record_run_start(
                self._db, job_id=job_id,
            )

            session_id = await runner.spawn(request)
            logger.info(
                "Dispatched user job %s '%s' (session=%s, run=%s)",
                job_id[:8], job.get("title", ""), session_id[:8], run_id[:8],
            )

            # Mark as passed (the session runs async — final result is unknown
            # until the session completes, but we record the dispatch as success)
            await crud.record_run_complete(
                self._db, run_id, status="passed",
                result_json={"session_id": session_id},
            )

            # Record job health for the retry registry
            with contextlib.suppress(Exception):
                rt.record_job_success(f"user_job:{job_id[:8]}")

            return session_id

        except Exception as exc:
            logger.exception("User job dispatch failed for %s", job_id[:8])
            # Record failure — reuse existing run_id if we already started one
            try:
                if run_id:
                    await crud.record_run_complete(
                        self._db, run_id, status="failed",
                        error_message=str(exc),
                    )
                else:
                    run_id = await crud.record_run_start(self._db, job_id=job_id)
                    await crud.record_run_complete(
                        self._db, run_id, status="failed",
                        error_message=str(exc),
                    )
            except Exception:
                logger.debug("Failed to record run failure", exc_info=True)

            with contextlib.suppress(Exception):
                from genesis.runtime import GenesisRuntime
                rt = GenesisRuntime.instance()
                rt.record_job_failure(f"user_job:{job_id[:8]}", str(exc))

            return None
