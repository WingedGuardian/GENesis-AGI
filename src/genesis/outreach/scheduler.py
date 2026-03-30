"""Outreach scheduler — morning report, surplus outreach, health alerts, engagement polling."""

from __future__ import annotations

import logging

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from genesis.outreach.config import OutreachConfig
from genesis.outreach.engagement import EngagementTracker
from genesis.outreach.morning_report import MorningReportGenerator
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.types import OutreachCategory, OutreachRequest

logger = logging.getLogger(__name__)


class OutreachScheduler:
    """Owns APScheduler jobs for outreach: morning report, surplus, engagement."""

    def __init__(
        self,
        pipeline: OutreachPipeline,
        morning_report: MorningReportGenerator,
        engagement: EngagementTracker,
        config: OutreachConfig,
        db: aiosqlite.Connection,
        *,
        reconciler: object | None = None,
        curve_computer: object | None = None,
        event_bus: object | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._morning = morning_report
        self._engagement = engagement
        self._config = config
        self._db = db
        self._reconciler = reconciler
        self._curve_computer = curve_computer
        self._event_bus = event_bus
        self._scheduler: AsyncIOScheduler | None = None

    @property
    def is_running(self) -> bool:
        """Whether the APScheduler event loop is active."""
        return self._scheduler is not None

    def start(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            logger.warning("OutreachScheduler.start() called while already running — restarting")
        self._scheduler = AsyncIOScheduler()
        hour, minute = self._config.morning_report_time.split(":")
        self._scheduler.add_job(
            self._morning_report_job,
            "cron",
            hour=int(hour),
            minute=int(minute),
            timezone=self._config.morning_report_timezone,
            id="outreach_morning_report",
            replace_existing=True,
        )
        # NOTE: _surplus_outreach_job registration REMOVED — surplus findings
        # must not reach user without passing through Genesis proper (executor).
        # Findings are staged in surplus_insights for reflection to review.
        self._scheduler.add_job(
            self._engagement_poll_job,
            "interval",
            minutes=self._config.engagement_poll_minutes,
            id="outreach_engagement_poll",
            replace_existing=True,
        )
        # Daily calibration — reconcile predictions + recompute curves
        # Wrap minute to avoid APScheduler crash if morning_report_time >= XX:55
        cal_raw = int(minute) + 5
        cal_hour = int(hour) + (cal_raw // 60)
        cal_minute = cal_raw % 60
        self._scheduler.add_job(
            self._calibration_job,
            "cron",
            hour=cal_hour,
            minute=cal_minute,
            timezone=self._config.morning_report_timezone,
            id="outreach_calibration",
            replace_existing=True,
        )
        # Health check — surfaces critical infrastructure problems to user
        self._scheduler.add_job(
            self._health_check_job,
            "interval",
            minutes=30,
            id="outreach_health_check",
            replace_existing=True,
        )
        # Drain pending outreach queue — picks up messages from foreground sessions
        self._scheduler.add_job(
            self._drain_pending_job,
            "interval",
            minutes=5,
            id="outreach_drain_pending",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("OutreachScheduler started (morning=%s:%s %s, engagement=%dm, health=30m, drain=5m)",
                     hour, minute, self._config.morning_report_timezone,
                     self._config.engagement_poll_minutes)

    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    async def _record_job_result(self, name: str, *, error: str | None = None) -> None:
        """Record success/failure in runtime + emit event."""
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if error:
            rt.record_job_failure(name, error)
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem

                await self._event_bus.emit(
                    Subsystem.OUTREACH, Severity.ERROR,
                    f"{name}.failed", f"Scheduled job {name} failed: {error}",
                )
        else:
            rt.record_job_success(name)
            # Heartbeat — lets health MCP detect silent death
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem

                await self._event_bus.emit(
                    Subsystem.OUTREACH, Severity.DEBUG,
                    "heartbeat", f"{name} completed",
                )

    @staticmethod
    def _is_paused() -> bool:
        try:
            from genesis.runtime import GenesisRuntime
            return GenesisRuntime.instance().paused
        except Exception:
            return False

    async def _morning_report_job(self) -> None:
        if self._is_paused():
            logger.debug("Morning report skipped (Genesis paused)")
            return
        try:
            req = await self._morning.generate()
            result = await self._pipeline.submit(req)
            logger.info("Morning report: %s", result.status.value)

            # Auto-acknowledge digest so it doesn't appear as "urgent unread"
            if result.outreach_id and result.status.value == "delivered":
                try:
                    from genesis.db.crud.outreach import record_engagement

                    await record_engagement(
                        self._pipeline._db,
                        result.outreach_id,
                        engagement_outcome="ambivalent",
                        engagement_signal="auto_digest",
                    )
                except Exception:
                    logger.warning(
                        "Failed to auto-acknowledge morning report",
                        exc_info=True,
                    )

            await self._record_job_result("morning_report")
        except Exception as exc:
            logger.exception("Morning report job failed")
            await self._record_job_result("morning_report", error=str(exc))

    async def _surplus_outreach_job(self) -> None:
        if self._is_paused():
            logger.debug("Surplus outreach skipped (Genesis paused)")
            return
        try:
            insight = await self._pick_best_insight()
            if not insight:
                logger.info("No pending surplus insights for daily outreach")
                await self._record_job_result("surplus_outreach")
                return
            req = OutreachRequest(
                category=OutreachCategory.SURPLUS,
                topic=insight["content"][:100],
                context=insight["content"],
                salience_score=insight.get("confidence", 0.7),
                signal_type="surplus_insight",
                drive_alignment=insight.get("drive_alignment"),
                labeled_surplus=True,
                source_id=insight["id"],
            )
            result = await self._pipeline.submit(req)
            if result.status.value == "delivered":
                await self._db.execute(
                    "UPDATE surplus_insights SET promotion_status = 'promoted', "
                    "promoted_to = ? WHERE id = ?",
                    (result.outreach_id, insight["id"]),
                )
                await self._db.commit()
            logger.info("Surplus outreach: %s (insight=%s)", result.status.value, insight["id"])
            await self._record_job_result("surplus_outreach")
        except Exception as exc:
            logger.exception("Surplus outreach job failed")
            await self._record_job_result("surplus_outreach", error=str(exc))

    async def _engagement_poll_job(self) -> None:
        if self._is_paused():
            return
        try:
            count = await self._engagement.check_timeouts(
                timeout_hours=self._config.engagement_timeout_hours,
            )
            if count:
                logger.info("Engagement poll: %d items timed out", count)
            await self._record_job_result("engagement_poll")
        except Exception as exc:
            logger.exception("Engagement poll failed")
            await self._record_job_result("engagement_poll", error=str(exc))

    async def _health_check_job(self) -> None:
        """Check health alerts and send outreach for critical issues.

        Note: health checks still run when paused — they're observability, not dispatches.

        Only alerts in the immediate_escalation whitelist (CRITICAL severity)
        reach Telegram. Everything else stays internal (dashboard, morning
        report, awareness signals). Multiple alerts are batched into one message.
        """
        try:
            from datetime import UTC, datetime

            from genesis.outreach.health_outreach import HealthOutreachBridge

            escalation_ids = frozenset(
                self._config.immediate_escalation_alerts
            )
            bridge = HealthOutreachBridge(self._db, escalation_ids=escalation_ids)
            requests = await bridge.check_and_generate()

            if not requests:
                await self._record_job_result("health_check")
                return

            # Batch all immediate alerts into one Telegram message
            lines = ["\u26a0\ufe0f INFRASTRUCTURE ALERT", ""]
            for req in requests:
                lines.append(f"\U0001f534 {req.context}")
            lines.append("")
            lines.append(
                f"({len(requests)} critical alert(s) at "
                f"{datetime.now(UTC).strftime('%H:%M UTC')})"
            )
            batched_text = "\n".join(lines)

            # Use BLOCKER category for the batched envelope
            envelope = OutreachRequest(
                category=OutreachCategory.BLOCKER,
                topic="Infrastructure Alert (batched)",
                context=batched_text,
                salience_score=1.0,
                signal_type="health_alert",
                source_id=",".join(r.source_id or "" for r in requests),
            )

            result = await self._pipeline.submit_raw(batched_text, envelope)
            logger.info(
                "Health outreach (batched %d alert(s)): %s",
                len(requests), result.status.value,
            )
            await self._record_job_result("health_check")
        except Exception as exc:
            logger.exception("Health check outreach job failed")
            await self._record_job_result("health_check", error=str(exc))

    async def _calibration_job(self) -> None:
        """Reconcile predictions and recompute calibration curves."""
        if self._is_paused():
            return
        try:
            if self._reconciler:
                results = await self._reconciler.reconcile_all()
                logger.info("Calibration reconciliation: %s", results)
            if self._curve_computer:
                for domain in ("outreach", "triage", "procedure", "routing"):
                    await self._curve_computer.compute_and_save(domain)
                logger.info("Calibration curves recomputed")
            await self._record_job_result("calibration")
        except Exception as exc:
            logger.exception("Calibration job failed")
            await self._record_job_result("calibration", error=str(exc))

    async def _drain_pending_job(self) -> None:
        """Drain pending_outreach table — deliver queued messages from foreground sessions."""
        if self._is_paused():
            return
        try:
            from datetime import UTC, datetime

            from genesis.db.crud import pending_outreach

            now = datetime.now(UTC).isoformat()
            rows = await pending_outreach.drain(self._db, now=now)
            if not rows:
                await self._record_job_result("drain_pending")
                return

            for row in rows:
                try:
                    # Validate category — fall back to ALERT for unknown categories
                    try:
                        cat = OutreachCategory(row["category"])
                    except ValueError:
                        logger.warning("Unknown category '%s' in pending outreach %s, using ALERT",
                                       row["category"], row["id"])
                        cat = OutreachCategory.ALERT
                    req = OutreachRequest(
                        category=cat,
                        topic=row["message"][:100],
                        context=row["message"],
                        salience_score=0.7,
                        signal_type="pending_queue",
                        channel=row.get("channel", "telegram"),
                    )
                    if row.get("urgency") == "high":
                        result = await self._pipeline.submit_urgent(req)
                    else:
                        result = await self._pipeline.submit(req)
                    logger.info(
                        "Drained pending outreach %s: %s",
                        row["id"], result.status.value,
                    )
                    # Only mark delivered on success
                    delivered_at = datetime.now(UTC).isoformat()
                    await pending_outreach.mark_delivered(
                        self._db, row["id"], delivered_at=delivered_at,
                    )
                except Exception:
                    logger.error(
                        "Failed to deliver pending outreach %s — will retry next cycle",
                        row["id"], exc_info=True,
                    )

            await self._record_job_result("drain_pending")
        except Exception as exc:
            logger.exception("Drain pending outreach job failed")
            await self._record_job_result("drain_pending", error=str(exc))

    async def _pick_best_insight(self) -> dict | None:
        cursor = await self._db.execute(
            "SELECT id, content, confidence, drive_alignment FROM surplus_insights "
            "WHERE promotion_status = 'pending' "
            "AND ttl > datetime('now') "
            "ORDER BY confidence DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {"id": row[0], "content": row[1], "confidence": row[2], "drive_alignment": row[3]}
