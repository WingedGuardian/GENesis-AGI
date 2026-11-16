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
import json
import logging
from typing import TYPE_CHECKING

from genesis.db.crud.cc_sessions import OUTCOMES as _SESSION_OUTCOMES

if TYPE_CHECKING:
    import aiosqlite

    from genesis.observability.event_bus import GenesisEventBus

logger = logging.getLogger(__name__)


def session_outcome(
    row: dict | None,
    *,
    session_id: str | None = None,
) -> tuple[str, str | None, dict] | None:
    """Map one session row to a terminal user-job run result, if terminal."""
    if row is None:
        result = {
            "session_id": session_id,
            "session_status": None,
            "session_outcome": None,
            "session_outcome_reason": None,
        }
        return "failed", "session record not found", result

    status = row.get("status")
    if status not in {"completed", "failed", "expired"}:
        return None

    outcome = row.get("outcome")
    outcome_reason = row.get("outcome_reason")
    result = {
        "session_id": row.get("id") or session_id,
        "session_status": status,
        "session_outcome": outcome,
        "session_outcome_reason": outcome_reason,
    }

    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = None
    if not isinstance(metadata, dict):
        metadata = {}
    if "park_id" in metadata:
        result["park_id"] = metadata["park_id"]

    metadata_error = metadata.get("error")
    metadata_error = (
        metadata_error if isinstance(metadata_error, str) and metadata_error else None
    )
    reason = outcome_reason if isinstance(outcome_reason, str) and outcome_reason else None
    passed = status == "completed" and outcome == "finished" and not metadata_error
    if passed:
        return "passed", None, result

    if metadata_error:
        error = metadata_error
    elif reason:
        error = reason
    elif status == "expired" or outcome == "expired":
        error = "session expired before reporting a result; outcome unknown"
    elif outcome is None or outcome == "unreported":
        error = "session outcome unknown/unreported; execution failure is not confirmed"
    elif outcome not in _SESSION_OUTCOMES:
        error = "session outcome unknown; execution failure is not confirmed"
    elif status == "failed" or outcome == "failed":
        error = "session failed"
    elif outcome == "blocked":
        error = "session reported blocked"
    elif outcome == "partial":
        error = "session result incomplete (partial)"
    elif outcome == "parked":
        error = "session parked for rate limit"
    else:
        error = f"session ended with status {status} and outcome {outcome}"
    return "failed", error[:2000], result


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

    def is_registered(self, job_id: str) -> bool:
        """Report current runtime membership, separately from saved active intent."""
        return self.is_running and self._scheduler.get_job(f"user_job:{job_id}") is not None

    async def start(self) -> None:
        """Load active jobs from DB and start the APScheduler."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler()

        from genesis.db.crud import user_jobs as crud

        try:
            await self.reconcile_open_runs()
        except Exception:
            logger.exception("User-job boot reconciliation failed")
        jobs = await crud.list_jobs(self._db, status="active")
        for job in jobs:
            self._register_job(job)

        self._scheduler.start()
        registered = sum(self.is_registered(job["id"]) for job in jobs)
        logger.info(
            "User job scheduler started with %d registered, %d pending job(s)",
            registered, len(jobs) - registered,
        )

    async def stop(self) -> None:
        """Shut down the APScheduler."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("User job scheduler stopped")

    async def _finalize_run(self, run: dict) -> bool:
        """Finalize a run from its session row with a running-status CAS."""
        from genesis.db.crud import cc_sessions
        from genesis.db.crud import user_jobs as crud

        session_id = run.get("session_id")
        if not session_id:
            return False
        row = await cc_sessions.get_by_id(self._db, session_id)
        mapped = session_outcome(row, session_id=session_id)
        if mapped is None:
            return False
        status, error, result = mapped
        updated = await crud.record_run_complete(
            self._db,
            run["id"],
            status=status,
            result_json=result,
            error_message=error,
            only_if_running=True,
        )
        if updated:
            logger.info(
                "Finalized user-job run %s session %s as %s",
                run["id"][:8], session_id[:8], status,
            )
        return updated

    async def on_session_end(self, session_id: str) -> None:
        """Finalize the user-job run attached to a terminal session, if any."""
        from genesis.db.crud import user_jobs as crud

        try:
            run = await crud.get_running_run_by_session(self._db, session_id)
            if run is not None:
                await self._finalize_run(run)
        except Exception:
            logger.exception("Failed to reconcile user-job run for session %s", session_id)

    async def reconcile_open_runs(self) -> int:
        """Catch up terminal sessions and interrupted runs without stopping startup."""
        from genesis.db.crud import user_jobs as crud

        try:
            runs = await crud.list_running_runs(self._db)
        except Exception:
            logger.exception("Failed to list running user-job runs for reconciliation")
            return 0

        finalized = 0
        for run in runs:
            try:
                session_id = run.get("session_id")
                if not session_id:
                    result = {
                        "session_id": None,
                        "session_status": None,
                        "session_outcome": None,
                        "session_outcome_reason": None,
                    }
                    updated = await crud.record_run_complete(
                        self._db,
                        run["id"],
                        status="failed",
                        result_json=result,
                        error_message="dispatch interrupted before a session was attached",
                        only_if_running=True,
                    )
                    if updated:
                        logger.info(
                            "Finalized user-job run %s session %s as failed",
                            run["id"][:8], "none",
                        )
                else:
                    updated = await self._finalize_run(run)
                if updated:
                    finalized += 1
            except Exception:
                logger.exception("Failed to reconcile user-job run %s", run.get("id"))
        if finalized:
            logger.info("Reconciled %d user-job run(s)", finalized)
        return finalized

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
        if not job or job["status"] != "active" or not self.is_running:
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
        """Save active intent and report whether registration succeeded."""
        from genesis.db.crud import user_jobs as crud

        updated = await crud.update_job(self._db, job_id, status="active")
        return await self.add_job(job_id) if updated else False

    async def run_now(self, job_id: str) -> str | None:
        """Immediately dispatch a job, bypassing the cron schedule.

        Returns the session ID if dispatched, or None on failure.
        """
        return await self._dispatch_job(job_id)

    async def _dispatch_job(self, job_id: str) -> str | None:
        """Dispatch a session and record its real outcome when it ends."""
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

            try:
                attached = await crud.attach_run_session(self._db, run_id, session_id)
                if not attached:
                    logger.warning(
                        "Could not attach session %s to user-job run %s",
                        session_id[:8], run_id[:8],
                    )
                else:
                    try:
                        await self._finalize_run({"id": run_id, "session_id": session_id})
                    except Exception:
                        logger.exception(
                            "Failed post-dispatch reconciliation for user-job run %s",
                            run_id[:8],
                        )
            except Exception:
                logger.exception(
                    "Failed to attach session %s to user-job run %s",
                    session_id[:8], run_id[:8],
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
                rt.record_job_failure(f"user_job:{job_id[:8]}", exc=exc)

            return None
