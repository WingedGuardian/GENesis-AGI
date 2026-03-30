"""Reflection scheduler — weekly self-assessment, quality calibration, regression checks."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.cc.reflection_bridge import CCReflectionBridge
    from genesis.reflection.stability import LearningStabilityMonitor

logger = logging.getLogger(__name__)


class ReflectionScheduler:
    """Owns APScheduler for weekly reflection jobs.

    - Weekly self-assessment: Sunday 10:00 UTC
    - Weekly quality calibration: Sunday 12:00 UTC (2h after assessment)
    - Learning regression check runs after quality calibration completes
    """

    def __init__(
        self,
        *,
        bridge: CCReflectionBridge,
        stability_monitor: LearningStabilityMonitor | None = None,
        db: aiosqlite.Connection,
        assessment_day: str = "sun",
        assessment_hour: int = 10,
        calibration_hour: int = 12,
        event_bus: object | None = None,
    ):
        self._bridge = bridge
        self._stability = stability_monitor
        self._db = db
        self._event_bus = event_bus
        self._scheduler = None
        self._assessment_day = assessment_day
        self._assessment_hour = assessment_hour
        self._calibration_hour = calibration_hour

    @property
    def is_running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    async def start(self) -> None:
        """Start the weekly reflection scheduler."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        self._scheduler = AsyncIOScheduler()

        self._scheduler.add_job(
            self._run_assessment,
            CronTrigger(
                day_of_week=self._assessment_day,
                hour=self._assessment_hour,
            ),
            id="weekly_self_assessment",
            max_instances=1,
            misfire_grace_time=None,
        )

        self._scheduler.add_job(
            self._run_calibration,
            CronTrigger(
                day_of_week=self._assessment_day,
                hour=self._calibration_hour,
            ),
            id="weekly_quality_calibration",
            max_instances=1,
            misfire_grace_time=None,
        )

        self._scheduler.start()
        logger.info(
            "Reflection scheduler started (assessment=%s@%02d, calibration=%s@%02d)",
            self._assessment_day, self._assessment_hour,
            self._assessment_day, self._calibration_hour,
        )

    async def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("Reflection scheduler stopped")

    async def _record_job_result(self, name: str, *, error: str | None = None) -> None:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if error:
            rt.record_job_failure(name, error)
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem

                await self._event_bus.emit(
                    Subsystem.REFLECTION, Severity.ERROR,
                    f"{name}.failed", f"Scheduled job {name} failed: {error}",
                )
        else:
            rt.record_job_success(name)
            # Heartbeat — lets health MCP detect silent death
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem

                await self._event_bus.emit(
                    Subsystem.REFLECTION, Severity.DEBUG,
                    "heartbeat", f"{name} completed",
                )

    async def _run_assessment(self) -> None:
        """Run weekly self-assessment with idempotency check."""
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Weekly assessment skipped (Genesis paused)")
                return
        except Exception:
            pass
        try:
            if await self._already_ran_this_week("self_assessment"):
                logger.info("Weekly assessment already ran this week — skipping")
                return

            result = await self._bridge.run_weekly_assessment(self._db)
            if result.success:
                logger.info("Weekly self-assessment completed")
                await self._record_job_result("weekly_assessment")
            else:
                logger.warning("Weekly self-assessment failed: %s", result.reason)
                await self._record_job_result("weekly_assessment", error=result.reason or "unknown")
        except Exception as exc:
            logger.exception("Weekly self-assessment error")
            await self._record_job_result("weekly_assessment", error=str(exc))

    async def _run_calibration(self) -> None:
        """Run quality calibration + regression check."""
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Quality calibration skipped (Genesis paused)")
                return
        except Exception:
            pass
        try:
            if await self._already_ran_this_week("quality_calibration"):
                logger.info("Quality calibration already ran this week — skipping")
                return

            result = await self._bridge.run_quality_calibration(self._db)
            if result.success:
                logger.info("Quality calibration completed")
            else:
                logger.warning("Quality calibration failed: %s", result.reason)

            # Run regression check after calibration
            if self._stability:
                regression = await self._stability.check_regression(self._db)
                if regression:
                    await self._stability.emit_regression_signal(self._db)
                    logger.warning("Learning regression detected after calibration")

            if result.success:
                await self._record_job_result("weekly_calibration")
            else:
                await self._record_job_result("weekly_calibration", error=result.reason or "unknown")
        except Exception as exc:
            logger.exception("Quality calibration error")
            await self._record_job_result("weekly_calibration", error=str(exc))

    async def _already_ran_this_week(self, obs_type: str) -> bool:
        """Check if an observation of this type exists from the current week."""
        from genesis.db.crud import observations

        # Get start of current week (Monday 00:00)
        now = datetime.now(UTC)
        monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
        monday -= __import__("datetime").timedelta(days=now.weekday())
        monday_iso = monday.isoformat()

        recent = await observations.query(self._db, type=obs_type, limit=1)
        if not recent:
            return False

        return recent[0].get("created_at", "") >= monday_iso
