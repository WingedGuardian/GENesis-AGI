"""Reflection scheduler — weekly self-assessment, quality calibration, regression checks."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.cc.reflection_bridge import CCReflectionBridge
    from genesis.reflection.stability import LearningStabilityMonitor

logger = logging.getLogger(__name__)


class ReflectionScheduler:
    """Owns APScheduler for the weekly reflection jobs.

    Both jobs FIRE DAILY (at the configured hours) but the
    ``_already_ran_this_week`` idempotency gate holds each to ≤1 SUCCESSFUL run
    per Monday-anchored week — so a day that fails (e.g. the shared Claude Code
    subscription is capped and returns empty output) is retried the next day
    instead of leaving a ≥7-day gap. Effective cadence: one success per week,
    normally landing on the week's first fire (Monday).

    - Self-assessment: daily @ ``assessment_hour`` (default 10)
    - Quality calibration: daily @ ``calibration_hour`` (default 12)
    - Learning regression check runs after a SUCCESSFUL quality calibration

    ``assessment_day`` is retained for back-compat but no longer pins the fire
    day. Timezone is resolved via ``genesis.env.user_timezone()`` (USER_TIMEZONE
    env → genesis.yaml timezone → UTC fallback).
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
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        from genesis.env import user_timezone

        tz_name = user_timezone()
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning("Invalid timezone %r, falling back to UTC", tz_name)
            tz = ZoneInfo("UTC")
            tz_name = "UTC"

        self._scheduler = AsyncIOScheduler()

        # Fire DAILY (at the configured hour) rather than only on one weekday,
        # and let the _already_ran_this_week() idempotency gate hold it to ≤1
        # SUCCESSFUL run per week. This self-heals: if the run fails on its
        # normal day (e.g. the shared Claude Code subscription is capped and
        # returns empty output), it retries the next day — and the next —
        # until one success lands that week, instead of leaving a ≥7-day gap.
        # Effective cadence: one success per Monday-anchored week (normally the
        # week's first fire); later days that week no-op via the gate.
        # (`_assessment_day` is retained for config/back-compat but no longer
        # pins the fire day — the idempotency week anchor governs cadence.)
        self._scheduler.add_job(
            self._run_assessment,
            CronTrigger(hour=self._assessment_hour, timezone=tz),
            id="weekly_self_assessment",
            max_instances=1,
            misfire_grace_time=None,
        )

        self._scheduler.add_job(
            self._run_calibration,
            CronTrigger(hour=self._calibration_hour, timezone=tz),
            id="weekly_quality_calibration",
            max_instances=1,
            misfire_grace_time=None,
        )

        self._scheduler.start()
        logger.info(
            "Reflection scheduler started (daily fire, ≤1 success/week via idempotency; "
            "assessment@%02d, calibration@%02d, tz=%s)",
            self._assessment_hour, self._calibration_hour, tz_name,
        )

    async def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("Reflection scheduler stopped")

    async def _record_job_result(
        self, name: str, *, error: str | None = None, exc: BaseException | None = None
    ) -> None:
        """Record a job outcome.

        Pass *exc* whenever an exception caused the failure — it is what makes
        the event diagnosable (``error_type`` + frames). A failure reported only
        as a semantic *error* string (a job result's ``reason``, e.g. a provider
        quota block) carries no ``error_type``, which is how consumers tell an
        internal defect from an external blocker.
        """
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if error or exc is not None:
            from genesis.observability.failure_details import error_summary, failure_details

            rt.record_job_failure(
                name,
                error_summary(exc, error) or "unknown",
                error_type=type(exc).__name__ if exc is not None else None,
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem

                await self._event_bus.emit(
                    Subsystem.REFLECTION, Severity.ERROR,
                    f"{name}.failed", f"Scheduled job {name} failed: {error}",
                    **failure_details(exc=exc, reason=None if exc is not None else error),
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
            await self._record_job_result("weekly_assessment", error=str(exc), exc=exc)

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
            if await self._already_ran_this_week("quality_calibration", "quality_drift"):
                logger.info("Quality calibration already ran this week — skipping")
                return

            result = await self._bridge.run_quality_calibration(self._db)
            if result.success:
                logger.info("Quality calibration completed")
                # Regression check only after a SUCCESSFUL calibration — a failed
                # run produces no new effectiveness data to regress against, and
                # with daily firing an outage week would otherwise re-run this
                # check (and possibly re-emit the signal) every day.
                if self._stability:
                    regression = await self._stability.check_regression(self._db)
                    if regression:
                        await self._stability.emit_regression_signal(self._db)
                        logger.warning("Learning regression detected after calibration")
                    else:
                        # Resolve-on-recovery: clear any standing regression alarm
                        # once effectiveness recovers, so it doesn't persist stale.
                        await self._stability.resolve_regression_if_standing(self._db)
                await self._record_job_result("weekly_calibration")
            else:
                logger.warning("Quality calibration failed: %s", result.reason)
                await self._record_job_result("weekly_calibration", error=result.reason or "unknown")
        except Exception as exc:
            logger.exception("Quality calibration error")
            await self._record_job_result("weekly_calibration", error=str(exc), exc=exc)

    async def _already_ran_this_week(self, *obs_types: str) -> bool:
        """Check if an observation of any of these types exists from this week.

        Accepts multiple types because one logical run can land under more than
        one observation type: quality calibration writes ``quality_calibration``
        on a clean week but ``quality_drift`` on a drift week (see
        ``output_router.route_calibration``). The guard must recognise either —
        otherwise a drift week looks like "never ran" and the run could repeat.
        The observation is written only on a *successful* run (a failure writes
        nothing), so its presence is a last-success signal and a failed run
        stays retriable.

        The reflection jobs fire DAILY (see ``start``); this gate is what holds
        that to ≤1 successful run per Monday-anchored week. Once a week's run
        succeeds, later days no-op here; if the run fails, the window stays
        empty so the next day's fire retries — that is the self-heal.
        """
        from genesis.db.crud import observations

        # Get start of current week (Monday 00:00)
        now = datetime.now(UTC)
        monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
        monday -= timedelta(days=now.weekday())
        monday_iso = monday.isoformat()

        for obs_type in obs_types:
            recent = await observations.query(self._db, type=obs_type, limit=1)
            if recent and recent[0].get("created_at", "") >= monday_iso:
                return True

        return False
