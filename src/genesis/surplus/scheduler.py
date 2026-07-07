"""SurplusScheduler — orchestrates surplus compute dispatch with own APScheduler."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from genesis.db.crud import surplus as surplus_crud
from genesis.env import user_timezone
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.surplus.brainstorm import BrainstormRunner
from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.executor import StubExecutor
from genesis.surplus.idle_detector import IdleDetector
from genesis.surplus.jobs import dream as dream_jobs
from genesis.surplus.jobs import gates as gate_jobs
from genesis.surplus.jobs import gitnexus as gitnexus_jobs
from genesis.surplus.jobs import runners as runner_jobs

# Re-export: tests/test_surplus/test_gitnexus_strip.py imports the strip
# helper from its historical home here.
from genesis.surplus.jobs.gitnexus import _strip_gitnexus_block  # noqa: F401
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.types import (
    INSIGHT_PRODUCING_TASK_TYPES,
    SurplusExecutor,
    TaskType,
)

if TYPE_CHECKING:
    from genesis.memory.store import MemoryStore
    from genesis.recon.gatherer import ReconGatherer
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


def _restart_safe_hourly(hours: int, *, minute: int = 0):
    """Restart-safe replacement for ``IntervalTrigger(hours=N)``.

    A >1h IntervalTrigger measures from the last start and RESETS on every restart
    (the CLAUDE.md trap), so a server that restarts more often than N never fires the
    job. A CronTrigger fires on the wall clock and never resets. Callers keep their own
    ``_recently_completed(..., N)`` cooldown as the true cadence gate, so this trigger
    only needs to fire frequently ENOUGH: an every-N-hours step for sub-daily N, and a
    single daily fire for N >= 24 (a cadence the cooldown already rate-limits).
    """
    from apscheduler.triggers.cron import CronTrigger

    if hours >= 24:
        return CronTrigger(hour=4, minute=minute, timezone=user_timezone())
    return CronTrigger(hour=f"*/{max(1, hours)}", minute=minute, timezone=user_timezone())


class SurplusScheduler:
    """The surplus orchestrator — drives task dispatch on its own schedule.

    Owns a separate AsyncIOScheduler from the Awareness Loop.
    Two recurring jobs: brainstorm check (12h) and dispatch loop (5m).
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        queue: SurplusQueue,
        idle_detector: IdleDetector,
        compute_availability: ComputeAvailability,
        executor: SurplusExecutor | None = None,
        brainstorm_runner: BrainstormRunner | None = None,
        dispatch_interval_minutes: int = 5,
        brainstorm_check_hours: int = 12,
        task_expiry_hours: int = 72,
        terminal_retention_days: int = 30,
        code_audit_hours: int = 12,
        code_index_hours: int = 4,
        recon_gather_hours: int = 84,
        maintenance_hours: int = 6,
        analytical_hours: int = 24,
        follow_up_dispatch_minutes: int | None = None,
        memory_extraction_hours: int = 2,
        j9_eval_batch_hours: int = 24,
        model_eval_hours: int = 24,
        clock=None,
        event_bus: GenesisEventBus | None = None,
        enable_code_audits: bool = True,
    ):
        self._db = db
        self._event_bus = event_bus
        self._enable_code_audits = enable_code_audits
        self._queue = queue
        self._idle_detector = idle_detector
        self._compute = compute_availability
        self._executor = executor or StubExecutor()
        self._brainstorm_runner = brainstorm_runner or BrainstormRunner(
            db, queue, executor=self._executor, clock=clock,
        )
        self._dispatch_interval = dispatch_interval_minutes
        self._brainstorm_interval = brainstorm_check_hours
        self._task_expiry_hours = task_expiry_hours
        self._terminal_retention_days = terminal_retention_days
        self._code_audit_hours = code_audit_hours
        self._code_index_hours = code_index_hours
        self._recon_gather_hours = recon_gather_hours
        self._maintenance_hours = maintenance_hours
        self._analytical_hours = analytical_hours
        self._follow_up_dispatch_minutes = follow_up_dispatch_minutes or dispatch_interval_minutes
        self._memory_extraction_hours = memory_extraction_hours
        self._j9_eval_batch_hours = j9_eval_batch_hours
        self._model_eval_hours = model_eval_hours
        self._clock = clock or (lambda: datetime.now(UTC))
        # Dedicated per-task-type executors. dispatch_once falls back to
        # self._executor for any type without a registered entry.
        self._executors: dict[TaskType, SurplusExecutor] = {}
        self._recon_gatherer: ReconGatherer | None = None
        self._model_intelligence_job = None  # Set via set_model_intelligence_job()
        self._models_md_synthesis_job = None  # Set via set_models_md_synthesis_job()
        self._skill_security_scan_job = None  # Set via set_skill_security_scan_job()
        self._github_discovery_job = None  # Set via set_github_discovery_job()
        self._extraction_store: MemoryStore | None = None
        self._extraction_router: Router | None = None
        # Router for the measurement-only surplus quality judge (set via
        # set_judge_router). Independent of extraction deps so the judge is
        # available whenever a router exists; None => judge records NULL verdicts.
        self._judge_router: Router | None = None
        self._follow_up_dispatcher = None  # Set via set_follow_up_dispatcher()
        self._topic_manager = None
        self._scheduler = AsyncIOScheduler()
        self._job_event_loop: asyncio.AbstractEventLoop | None = None

    def set_topic_manager(self, manager) -> None:
        """Set TopicManager for routing surplus reflections to Telegram topics."""
        self._topic_manager = manager
        if hasattr(self._executor, "set_topic_manager"):
            self._executor.set_topic_manager(manager)

    def set_executor(self, executor) -> None:
        """Replace the current executor (e.g., swap StubExecutor for a real one)."""
        self._executor = executor
        self._brainstorm_runner._executor = executor
        # Propagate topic_manager to the new executor
        if self._topic_manager and hasattr(executor, "set_topic_manager"):
            executor.set_topic_manager(self._topic_manager)

    def set_code_audit_executor(self, executor) -> None:
        """Set a dedicated executor for CODE_AUDIT tasks."""
        self._executors[TaskType.CODE_AUDIT] = executor

    def set_code_index_executor(self, executor: SurplusExecutor) -> None:
        """Set a dedicated executor for CODE_INDEX tasks (no LLM, pure AST)."""
        self._executors[TaskType.CODE_INDEX] = executor

    def set_bookmark_enrichment_executor(self, executor: SurplusExecutor) -> None:
        """Set a dedicated executor for BOOKMARK_ENRICHMENT tasks."""
        self._executors[TaskType.BOOKMARK_ENRICHMENT] = executor

    def set_model_eval_executor(self, executor: SurplusExecutor) -> None:
        """Set a dedicated executor for MODEL_EVAL tasks."""
        self._executors[TaskType.MODEL_EVAL] = executor

    def set_j9_eval_batch_executor(self, executor: SurplusExecutor) -> None:
        """Set executor for J9_EVAL_BATCH tasks (daily memory relevance scoring)."""
        self._executors[TaskType.J9_EVAL_BATCH] = executor

    def set_fresh_session_test_executor(self, executor: SurplusExecutor) -> None:
        """Set executor for FRESH_SESSION_TEST tasks (weekly documentation quality diagnostic)."""
        self._executors[TaskType.FRESH_SESSION_TEST] = executor
        # Late-registration: if scheduler already started, add the job now
        if self._scheduler.running and not self._scheduler.get_job("schedule_fresh_session_test"):
            from apscheduler.triggers.cron import CronTrigger
            self._scheduler.add_job(
                self._schedule_fresh_session_test,
                CronTrigger(day_of_week="sat", hour=9, timezone=user_timezone()),
                id="schedule_fresh_session_test",
                max_instances=1,
                misfire_grace_time=3600,
            )

    def set_maintenance_executors(
        self,
        *,
        disk_cleanup: SurplusExecutor | None = None,
        backup_verification: SurplusExecutor | None = None,
        dead_letter_replay: SurplusExecutor | None = None,
        db_maintenance: SurplusExecutor | None = None,
    ) -> None:
        """Set dedicated executors for infrastructure maintenance tasks."""
        if disk_cleanup:
            self._executors[TaskType.DISK_CLEANUP] = disk_cleanup
        if backup_verification:
            self._executors[TaskType.BACKUP_VERIFICATION] = backup_verification
        if dead_letter_replay:
            self._executors[TaskType.DEAD_LETTER_REPLAY] = dead_letter_replay
        if db_maintenance:
            self._executors[TaskType.DB_MAINTENANCE] = db_maintenance

    def set_cc_memory_staleness_executor(self, executor: SurplusExecutor) -> None:
        """Set a dedicated executor for CC_MEMORY_STALENESS tasks."""
        self._executors[TaskType.CC_MEMORY_STALENESS] = executor

    def set_recon_gatherer(self, gatherer: ReconGatherer) -> None:
        """Set the recon gatherer for scheduled release checking."""
        self._recon_gatherer = gatherer

    def set_model_intelligence_job(self, job) -> None:
        """Set the ModelIntelligenceJob for scheduled model landscape scanning."""
        self._model_intelligence_job = job

    def set_models_md_synthesis_job(self, job) -> None:
        """Set the ModelsMdSynthesisJob for weekly models.md updates."""
        self._models_md_synthesis_job = job

    def set_skill_security_scan_job(self, job) -> None:
        """Set the SkillSecurityScanJob for the weekly skill-security scan."""
        self._skill_security_scan_job = job

    def set_github_discovery_job(self, job) -> None:
        """Set the GitHubDiscoveryJob for weekly curated repo discovery."""
        self._github_discovery_job = job

    def set_extraction_deps(
        self,
        *,
        store: MemoryStore,
        router: Router,
    ) -> None:
        """Set dependencies for the memory extraction job."""
        self._extraction_store = store
        self._extraction_router = router

    def set_judge_router(self, router: Router) -> None:
        """Wire the router used by the measurement-only surplus quality judge
        (surplus.quality_judge).

        Kept separate from set_extraction_deps so the judge is available whenever
        a router exists, even if memory extraction was not wired. If never set
        (degraded init with no router), the judge records NULL verdicts.
        """
        self._judge_router = router

    def set_follow_up_dispatcher(self, dispatcher) -> None:
        """Set the follow-up dispatcher for accountability tracking.

        Registers the dispatch job if the scheduler is already running.
        """
        self._follow_up_dispatcher = dispatcher
        # Register the job if the scheduler is already running (late wiring)
        if self._scheduler.running and not self._scheduler.get_job("follow_up_dispatch"):
            self._scheduler.add_job(
                self.dispatch_follow_ups,
                IntervalTrigger(minutes=self._follow_up_dispatch_minutes),
                id="follow_up_dispatch",
                max_instances=1,
                misfire_grace_time=60,
            )

    async def start(self) -> None:
        """Start the surplus scheduler with brainstorm check and dispatch jobs."""
        # Brainstorm check: twice daily (1am + 1pm local).
        # CronTrigger instead of IntervalTrigger — IntervalTrigger resets on
        # restart.  Config param brainstorm_check_hours is unused since this
        # conversion; the fixed schedule replaces the interval cadence.
        from apscheduler.triggers.cron import CronTrigger
        self._scheduler.add_job(
            self.brainstorm_check,
            CronTrigger(hour="1,13", minute=0, timezone=user_timezone()),
            id="surplus_brainstorm_check",
            max_instances=1,
            misfire_grace_time=3600,
        )
        self._scheduler.add_job(
            self._dispatch_loop,
            IntervalTrigger(minutes=self._dispatch_interval),
            id="surplus_dispatch",
            max_instances=1,
            misfire_grace_time=60,
        )
        if self._enable_code_audits:
            # CronTrigger not IntervalTrigger — same >1h restart-reset trap as
            # maintenance/code_index (code_audit_hours=12 by default). _code_audit_hours
            # stays the _recently_completed cooldown; the boot run is covered by the
            # immediate await in start() below.
            self._scheduler.add_job(
                self.schedule_code_audit,
                _restart_safe_hourly(self._code_audit_hours, minute=5),
                id="schedule_code_audit",
                max_instances=1,
                misfire_grace_time=300,
            )
        else:
            logger.info("Code audits disabled — skipping job registration")
        # CronTrigger not IntervalTrigger — a >1h IntervalTrigger resets on every
        # restart (CLAUDE.md trap), so code indexing starves if the server restarts
        # more often than code_index_hours. _code_index_hours stays the
        # _recently_completed cooldown; the boot run is covered by start()'s await below.
        self._scheduler.add_job(
            self.schedule_code_index,
            _restart_safe_hourly(self._code_index_hours, minute=10),
            id="schedule_code_index",
            max_instances=1,
            misfire_grace_time=300,
        )
        # Recon gather: Tue & Fri 1:45am local (~3.5 day cadence).
        # CronTrigger instead of IntervalTrigger — IntervalTrigger(hours=84)
        # never fires if server restarts within 84h.  Config param
        # recon_gather_hours is unused since this conversion.
        self._scheduler.add_job(
            self.run_recon_gather,
            CronTrigger(day_of_week="tue,fri", hour=1, minute=45, timezone=user_timezone()),
            id="recon_gather",
            max_instances=1,
            misfire_grace_time=3600,
        )
        # Weekly DB integrity check: Sunday 3am local. A DETERMINISTIC full
        # PRAGMA integrity_check that ALARMS on corruption. The
        # DbMaintenanceExecutor already reports a fast quick_check, but on the
        # probabilistic surplus cadence and advisory-only — corruption
        # detection must not depend on idle scheduling.
        self._scheduler.add_job(
            self.run_db_integrity_check,
            CronTrigger(day_of_week="sun", hour=3, timezone=user_timezone()),
            id="db_integrity_check",
            max_instances=1,
            misfire_grace_time=3600,
        )
        # Model intelligence: weekly Sunday 8am local (after dream cycle clears)
        if self._model_intelligence_job is not None:
            self._scheduler.add_job(
                self.run_model_intelligence,
                CronTrigger(day_of_week="sun", hour=8, timezone=user_timezone()),
                id="model_intelligence",
                max_instances=1,
                misfire_grace_time=3600,
            )
        # Models.md synthesis: weekly Sunday 10am — updates model catalog from recon.
        # Runs 2h after model_intelligence to consume its findings (soft dep).
        if self._models_md_synthesis_job is not None:
            self._scheduler.add_job(
                self.run_models_md_synthesis,
                CronTrigger(day_of_week="sun", hour=10, timezone=user_timezone()),
                id="models_md_synthesis",
                max_instances=1,
                misfire_grace_time=3600,
            )
        # Skill-security scan: weekly Monday 2am — audits installed skills via SkillSpector.
        if self._skill_security_scan_job is not None:
            self._scheduler.add_job(
                self.run_skill_security_scan,
                CronTrigger(day_of_week="mon", hour=2, timezone=user_timezone()),
                id="skill_security_scan",
                max_instances=1,
                misfire_grace_time=3600,
            )
        # GitHub Discovery: weekly Wednesday 6am — finds new repos in the user's
        # domains and files the top few to the recon triage queue for review.
        if self._github_discovery_job is not None:
            self._scheduler.add_job(
                self.run_github_discovery,
                CronTrigger(day_of_week="wed", hour=6, timezone=user_timezone()),
                id="github_discovery",
                max_instances=1,
                misfire_grace_time=3600,
            )
        # Dream cycle: weekly Sunday 4am — clustering + worklist persist
        from apscheduler.triggers.cron import CronTrigger
        self._scheduler.add_job(
            self.run_dream_cycle,
            CronTrigger(day_of_week="sun", hour=4, timezone=user_timezone()),
            id="dream_cycle",
            max_instances=1,
            misfire_grace_time=3600,
        )
        # Dream synthesis drain: daily 8am — merges a bounded, value-ranked
        # slice of the weekly worklist (SHADOW until the user-gated live flip),
        # spreading synthesis load across the week instead of a Sunday spike.
        # 8am leaves ~4h after the weekly scan starts; if the weekly is still
        # running, the drain skips via the heavy_workload guard.
        self._scheduler.add_job(
            self.run_dream_synthesis_drain,
            CronTrigger(hour=8, timezone=user_timezone()),
            id="dream_synthesis_drain",
            max_instances=1,
            misfire_grace_time=3600,
        )
        # GitNexus reindex: Mon & Thu 5am local (~72h apart).
        # Uses CronTrigger (not IntervalTrigger) — IntervalTrigger resets
        # on restart and would never fire if server restarts more often.
        self._scheduler.add_job(
            self.run_gitnexus_reindex,
            CronTrigger(day_of_week="mon,thu", hour=5, timezone=user_timezone()),
            id="gitnexus_reindex",
            max_instances=1,
            misfire_grace_time=3600,
        )
        # GitNexus CLAUDE.md strip: hourly. Decoupled from the reindex job above —
        # out-of-band reindexes (GitNexus's own staleness `analyze`) also re-inject
        # the block but never run that job's post-strip, so without this the block
        # would persist in CLAUDE.md until the next Mon/Thu reindex.
        self._scheduler.add_job(
            self.run_gitnexus_strip,
            CronTrigger(minute=0, timezone=user_timezone()),
            id="gitnexus_strip",
            max_instances=1,
            misfire_grace_time=300,
        )
        # Wing audit: twice-weekly memory taxonomy review (Tue & Fri 2am local).
        # Moved off Sunday to avoid dream cycle congestion.
        self._scheduler.add_job(
            self.schedule_wing_audit,
            CronTrigger(day_of_week="tue,fri", hour=2, timezone=user_timezone()),
            id="wing_audit",
            max_instances=1,
            misfire_grace_time=3600,
        )
        # CC memory staleness scan: weekly Wednesday 3am local.
        # Moved off Sunday to spread weekly jobs across the week.
        self._scheduler.add_job(
            self.schedule_cc_memory_staleness,
            CronTrigger(day_of_week="wed", hour=3, timezone=user_timezone()),
            id="cc_memory_staleness",
            max_instances=1,
            misfire_grace_time=3600,
        )
        # CronTrigger not IntervalTrigger — a >1h IntervalTrigger resets on every
        # restart (CLAUDE.md trap), starving maintenance (surplus TTL, pending_embeddings,
        # heartbeats, weak links, transcript archival) under frequent restarts.
        # _maintenance_hours stays the _recently_completed cooldown (the real cadence
        # gate); the boot run is covered by the immediate await in start() below.
        self._scheduler.add_job(
            self.schedule_maintenance,
            _restart_safe_hourly(self._maintenance_hours, minute=20),
            id="schedule_maintenance",
            max_instances=1,
            misfire_grace_time=300,
        )
        # Analytical tasks: daily 7am local — LLM-based gap clustering,
        # prompt effectiveness, anticipatory research.
        # CronTrigger instead of IntervalTrigger — IntervalTrigger resets on
        # restart.  Config param analytical_hours is unused since this
        # conversion.
        if self._analytical_hours > 0:
            self._scheduler.add_job(
                self.schedule_analytical,
                CronTrigger(hour=7, minute=0, timezone=user_timezone()),
                id="schedule_analytical",
                max_instances=1,
                misfire_grace_time=3600,
            )
        if self._j9_eval_batch_hours > 0:
            # CronTrigger instead of IntervalTrigger: IntervalTrigger resets
            # on server restart, so a 24h job never fires if the server
            # restarts more frequently.  Fixed hour ensures the batch runs
            # daily regardless of restart cadence.
            from apscheduler.triggers.cron import CronTrigger
            self._scheduler.add_job(
                self.schedule_j9_eval_batch,
                CronTrigger(hour=3, timezone=user_timezone()),  # 3 AM local daily
                id="schedule_j9_eval_batch",
                max_instances=1,
                misfire_grace_time=3600,
            )
        # Fresh session test: weekly Saturday 9 AM local.
        # Moved off Sunday to avoid dream cycle congestion.
        if self._executors.get(TaskType.FRESH_SESSION_TEST) is not None:
            self._scheduler.add_job(
                self._schedule_fresh_session_test,
                CronTrigger(day_of_week="sat", hour=9, timezone=user_timezone()),
                id="schedule_fresh_session_test",
                max_instances=1,
                misfire_grace_time=3600,
            )
        # Model eval: daily 7:30am local — model quality benchmarks.
        # CronTrigger instead of IntervalTrigger — IntervalTrigger resets on
        # restart.  Config param model_eval_hours is unused since this
        # conversion.
        if self._model_eval_hours > 0:
            self._scheduler.add_job(
                self.schedule_model_eval,
                CronTrigger(hour=7, minute=30, timezone=user_timezone()),
                id="schedule_model_eval",
                max_instances=1,
                misfire_grace_time=3600,
            )
        if self._follow_up_dispatcher is not None:
            self._scheduler.add_job(
                self.dispatch_follow_ups,
                IntervalTrigger(minutes=self._follow_up_dispatch_minutes),
                id="follow_up_dispatch",
                max_instances=1,
                misfire_grace_time=60,
            )
        # Register error listener so failed/missed jobs are observable.
        # The handler is sync (runs in the scheduler thread); async work
        # is bridged via call_soon_threadsafe, matching AwarenessLoop's
        # pattern (see genesis.awareness.loop._on_scheduler_job_event).
        try:
            from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
            self._job_event_loop = asyncio.get_running_loop()
            self._scheduler.add_listener(
                self._on_scheduler_job_event,
                EVENT_JOB_ERROR | EVENT_JOB_MISSED,
            )
        except Exception:
            logger.warning("Failed to register scheduler error listener", exc_info=True)

        self._scheduler.start()

        # Emit initial heartbeats so the watchdog doesn't see stale
        # timestamps from the previous process and trigger a restart.
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            rt.record_job_success("surplus_dispatch")
            logger.info("Surplus scheduler: initial heartbeat emitted")
        except Exception:
            logger.warning("Could not emit initial heartbeat", exc_info=True)

        # Check for incomplete dream cycle runs from previous process.
        try:
            from genesis.memory.dream_cycle import check_incomplete_runs
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            if rt.db is not None:
                await check_incomplete_runs(rt.db)
        except Exception:
            logger.debug("Dream cycle integrity check skipped", exc_info=True)

        # Run brainstorm check immediately on startup
        await self.brainstorm_check()
        # Run remaining jobs immediately on startup —
        # otherwise they only fire at their next scheduled time.
        if self._enable_code_audits:
            await self.schedule_code_audit()
        await self.schedule_code_index()
        await self.run_recon_gather()
        await self.schedule_maintenance()
        if self._j9_eval_batch_hours > 0:
            await self.schedule_j9_eval_batch()
        if self._model_eval_hours > 0:
            await self.schedule_model_eval()
        if self._analytical_hours > 0:
            await self.schedule_analytical()
        # Clean any GitNexus block an out-of-band reindex left in CLAUDE.md.
        await self.run_gitnexus_strip()
        logger.info(
            "Surplus scheduler started (dispatch=%dm, brainstorm=%dh)",
            self._dispatch_interval, self._brainstorm_interval,
        )

    async def stop(self) -> None:
        """Stop the scheduler, waiting for any running job to finish."""
        self._scheduler.shutdown(wait=True)
        logger.info("Surplus scheduler stopped")

    # ── APScheduler error/missed listener ───────────────────────────────

    def _on_scheduler_job_event(self, event) -> None:
        """APScheduler listener — runs in the scheduler thread (sync).

        Hands the event off to the asyncio loop so async work (DB writes,
        event bus) can run safely.  Pattern copied from AwarenessLoop.
        """
        job_id = getattr(event, "job_id", "unknown")
        loop = self._job_event_loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(
                lambda jid=job_id, ev=event: asyncio.ensure_future(
                    self._emit_job_error_event(jid, ev)
                ),
            )
        except Exception:
            logger.warning(
                "Failed to hand off scheduler event for %s", job_id,
                exc_info=True,
            )

    async def _emit_job_error_event(self, job_id: str, event) -> None:
        """Emit observability event for a failed or missed scheduled job."""
        exception = getattr(event, "exception", None)
        is_error = exception is not None
        msg = (
            f"Scheduled job '{job_id}' failed: {exception}"
            if is_error
            else f"Scheduled job '{job_id}' missed (past misfire grace time)"
        )
        if is_error:
            logger.error(msg)
        else:
            logger.warning(msg)

        # Record failure in job health tracking
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            rt.record_job_failure(job_id, str(exception or "missed")[:500])
        except Exception:
            logger.warning("Failed to record job failure for %s", job_id, exc_info=True)

        # Emit to event bus for dashboard / alerting
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            if rt.event_bus:
                await rt.event_bus.emit(
                    Subsystem.SURPLUS,
                    Severity.ERROR if is_error else Severity.WARNING,
                    "scheduler.job_failed" if is_error else "scheduler.job_missed",
                    msg,
                    job_id=job_id,
                )
        except Exception:
            logger.warning("Failed to emit scheduler error event", exc_info=True)

    # ── Job delegates ────────────────────────────────────────────────────
    # Bodies live in genesis.surplus.jobs.*; each method here keeps the
    # original name so APScheduler job callables, runtime wiring
    # (_core.py JobRetryRegistry, init/surplus.py memory-extraction coro),
    # and tests keep working unchanged. Full docstrings live on the job
    # functions.

    async def brainstorm_check(self) -> None:
        """Ensure today's brainstorm sessions are queued."""
        await gate_jobs.brainstorm_check(self)

    async def _recently_completed(
        self, task_type, cooldown_hours: int | float,
    ) -> bool:
        """Return ``True`` if *task_type* completed within *cooldown_hours*."""
        return await gate_jobs.recently_completed(self, task_type, cooldown_hours)

    async def schedule_code_audit(self) -> None:
        """Enqueue a code audit task if none pending/running."""
        await gate_jobs.schedule_code_audit(self)

    async def schedule_code_index(self) -> None:
        """Enqueue a code index task if none pending/running."""
        await gate_jobs.schedule_code_index(self)

    async def schedule_j9_eval_batch(self) -> None:
        """Enqueue a J9 eval batch task if none pending/running."""
        await gate_jobs.schedule_j9_eval_batch(self)

    async def _schedule_fresh_session_test(self) -> None:
        """Enqueue a FRESH_SESSION_TEST task if none pending/running."""
        await gate_jobs.schedule_fresh_session_test(self)

    async def schedule_model_eval(self) -> None:
        """Enqueue a MODEL_EVAL task if none pending/running."""
        await gate_jobs.schedule_model_eval(self)

    async def schedule_maintenance(self) -> None:
        """Enqueue mechanical infrastructure maintenance tasks if none active."""
        await gate_jobs.schedule_maintenance(self)

    async def schedule_analytical(self) -> None:
        """Enqueue LLM-based analytical tasks if none active."""
        await gate_jobs.schedule_analytical(self)

    async def schedule_wing_audit(self) -> None:
        """Enqueue a wing audit task if none pending/running."""
        await gate_jobs.schedule_wing_audit(self)

    async def run_db_integrity_check(self) -> None:
        """Weekly full PRAGMA integrity_check with an alarm on corruption."""
        await runner_jobs.run_db_integrity_check(self)

    async def _alarm_db_integrity(self, detail: str) -> None:
        """Persist + broadcast a DB-corruption alarm (observation + ERROR event)."""
        await runner_jobs.alarm_db_integrity(self, detail)

    async def schedule_cc_memory_staleness(self) -> None:
        """Enqueue a CC memory staleness scan if none pending/running."""
        await gate_jobs.schedule_cc_memory_staleness(self)

    async def schedule_pipeline(self, pipeline_name: str) -> str | None:
        """Enqueue step 1 of a named pipeline if not already running."""
        return await gate_jobs.schedule_pipeline(self, pipeline_name)

    async def dispatch_follow_ups(self) -> None:
        """Run the follow-up dispatcher cycle (always-on, not idle-gated)."""
        await runner_jobs.dispatch_follow_ups(self)

    async def run_recon_gather(self) -> None:
        """Check watchlist projects for new GitHub releases and star counts."""
        await runner_jobs.run_recon_gather(self)

    async def run_model_intelligence(self) -> None:
        """Run model intelligence scan (weekly)."""
        await runner_jobs.run_model_intelligence(self)

    async def run_skill_security_scan(self) -> None:
        """Run the weekly skill-security scan (SkillSpector → recon findings)."""
        await runner_jobs.run_skill_security_scan(self)

    async def run_github_discovery(self) -> None:
        """Run weekly curated GitHub Discovery (new repos → recon triage queue)."""
        await runner_jobs.run_github_discovery(self)

    async def run_models_md_synthesis(self) -> None:
        """Run weekly models.md synthesis (Sunday 8am UTC)."""
        await runner_jobs.run_models_md_synthesis(self)

    async def run_dream_cycle(self) -> None:
        """Run the WEEKLY dream-cycle clustering pass (Sunday 4am)."""
        await dream_jobs.run_dream_cycle()

    async def run_dream_synthesis_drain(self) -> None:
        """Drain a bounded slice of the dream-cycle synthesis worklist (daily 8am)."""
        await dream_jobs.run_dream_synthesis_drain()

    async def run_gitnexus_reindex(self) -> None:
        """Reindex the GitNexus code graph (Mon & Thu 5am UTC)."""
        await gitnexus_jobs.run_gitnexus_reindex()

    async def run_gitnexus_strip(self) -> None:
        """Strip GitNexus's auto-injected block from CLAUDE.md (hourly + on startup)."""
        await gitnexus_jobs.run_gitnexus_strip()

    async def run_memory_extraction(self) -> None:
        """Run periodic memory extraction from session transcripts."""
        await runner_jobs.run_memory_extraction(self)

    async def dispatch_once(self) -> bool:
        """Single dispatch cycle. Returns True if a task was processed."""
        # 0. Recover tasks stuck in 'running' state (crashed mid-execution)
        await self._queue.recover_stuck()

        # 1. Drain expired pending tasks
        await self._queue.drain_expired(max_age_hours=self._task_expiry_hours)

        # 1b. Age-cap terminal rows (completed/failed/cancelled) so they don't
        # accumulate forever — drain_expired only touches pending.
        await self._queue.reap_terminal(older_than_days=self._terminal_retention_days)

        # 2. Check idle
        if not self._idle_detector.is_idle():
            return False

        # 3. Check compute availability
        available_tiers = await self._compute.get_available_tiers()

        # 4. Get next task
        task = await self._queue.next_task(available_tiers)
        if task is None:
            return False

        # 5. Execute
        logger.info("Dispatching surplus task %s (%s)", task.id, task.task_type)
        await self._queue.mark_running(task.id)

        from genesis.surplus.types import TaskType as _TT

        # Dedicated executor for this task type if registered; the default
        # executor otherwise (a registered-but-None entry also falls back,
        # matching the old per-slot `is not None` checks).
        executor = self._executors.get(task.task_type)
        if executor is None:
            executor = self._executor

        try:
            result = await executor.execute(task)
        except Exception:
            logger.exception("Surplus task %s failed with exception", task.id)
            await self._queue.mark_failed(task.id, reason="executor_exception")
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.SURPLUS, Severity.WARNING,
                    "task.failed",
                    f"Surplus task {task.id} failed with exception",
                    task_id=task.id, task_type=str(task.task_type),
                )
            # Signal autonomy correction for background cognitive failure
            try:
                from genesis.runtime import GenesisRuntime
                rt = GenesisRuntime.instance()
                mgr = getattr(rt, "_autonomy_manager", None)
                if mgr is not None:
                    from datetime import UTC, datetime
                    await mgr.record_correction(
                        "background_cognitive",
                        corrected_at=datetime.now(UTC).isoformat(),
                    )
            except Exception:
                logger.debug("Autonomy correction signal failed (non-fatal)", exc_info=True)
            await self._maybe_observe_failure(task, "executor_exception")
            return False

        if not result.success:
            await self._queue.mark_failed(task.id, reason=result.error or "unknown")
            # Signal autonomy correction for background cognitive failure
            try:
                from genesis.runtime import GenesisRuntime
                rt = GenesisRuntime.instance()
                mgr = getattr(rt, "_autonomy_manager", None)
                if mgr is not None:
                    from datetime import UTC, datetime
                    await mgr.record_correction(
                        "background_cognitive",
                        corrected_at=datetime.now(UTC).isoformat(),
                    )
            except Exception:
                logger.debug("Autonomy correction signal failed (non-fatal)", exc_info=True)
            await self._maybe_observe_failure(task, result.error or "unknown")
            return False

        # 6. Route through intake pipeline (atomize → score → route to knowledge)
        staging_id = None
        # Verified-correctness verdict (insight-producing types only), produced by
        # the measurement-only quality judge (surplus.quality_judge). Stays NULL for
        # action tasks, empty/too-short output, unknown types, and judge outages —
        # all ambiguous or not-a-quality-signal — so they keep the positive-only
        # behaviour. 'useful' = judge passed the output; 'hollow' = judge failed it
        # (harvested as a VERIFICATION_FAILED negative). judge_score/judge_detail
        # persist the continuous score + rationale for calibration/display.
        outcome_quality: str | None = None
        judge_score: float | None = None
        judge_detail: str | None = None
        if result.insights:
            insight = result.insights[0]
            content = result.content or ""
            # Quality gate: skip trivially short or empty insights
            if len(content.strip()) < 50:
                logger.warning(
                    "Surplus insight too short, skipping (%d chars, task=%s)",
                    len(content.strip()), task.id[:8],
                )
            else:
                if task.task_type == _TT.CODE_AUDIT:
                    # Code-audit output is ingested per-finding by
                    # FindingsBridge below, behind its confidence gate and
                    # slop filter. Routing the raw findings array through
                    # generic intake as well would double-ingest every
                    # finding and bypass both gates. Synthetic staging_id
                    # for tracking; the quality judge below still runs.
                    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                    staging_id = f"{task.task_type.value}-{content_hash}"
                else:
                    try:
                        from genesis.surplus.intake import (
                            run_intake,
                            source_for_task_type,
                        )
                        source = source_for_task_type(str(task.task_type))
                        intake_stats = await run_intake(
                            content=content,
                            source=source,
                            source_task_type=str(task.task_type),
                            generating_model=insight.get("generating_model", "unknown"),
                            db=self._db,
                        )
                        logger.info(
                            "Intake routed %d findings (k=%d, o=%d, d=%d) for task %s",
                            intake_stats.findings_count,
                            intake_stats.routed_knowledge,
                            intake_stats.routed_observation,
                            intake_stats.routed_discard,
                            task.id[:8],
                        )
                        # Use a synthetic staging_id for tracking
                        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                        staging_id = f"{task.task_type.value}-{content_hash}"
                    except Exception:
                        # Fallback: write to surplus_insights staging (old behavior)
                        logger.warning(
                            "Intake pipeline failed — falling back to surplus_insights staging",
                            exc_info=True,
                        )
                        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                        staging_id = f"{task.task_type.value}-{content_hash}"
                        now = self._clock()
                        ttl = (now + timedelta(days=7)).isoformat()
                        now_iso = now.isoformat()
                        await surplus_crud.upsert(
                            self._db,
                            id=staging_id,
                            content=content,
                            source_task_type=str(task.task_type),
                            generating_model=insight.get("generating_model", "unknown"),
                            drive_alignment=task.drive_alignment,
                            confidence=insight.get("confidence", 0.0),
                            created_at=now_iso,
                            ttl=ttl,
                        )

                # Measurement-only verified-correctness verdict. Runs whether
                # intake succeeded or fell back (content is valid to judge either
                # way); insight-producing types only. Grades the FULL output with
                # the eval LLM-judge — decoupled from intake routing (curated
                # sources stay trusted for storage; the judge only measures quality
                # so the Outcome Bus gains a real two-sided signal). NEVER raises;
                # a judge outage yields a NULL verdict, never a false 'hollow'.
                if task.task_type in INSIGHT_PRODUCING_TASK_TYPES:
                    from genesis.surplus.quality_judge import run_quality_judge
                    outcome_quality, judge_score, judge_detail = (
                        await run_quality_judge(
                            content, task.task_type, self._judge_router,
                        )
                    )

        await self._queue.mark_completed(
            task.id, staging_id=staging_id, outcome_quality=outcome_quality,
            judge_score=judge_score, judge_detail=judge_detail,
        )

        # Pipeline chaining — enqueue next step if this was a pipeline task.
        # Note: if a step returns NOMINAL (empty content), chaining still
        # proceeds — the next step gets empty previous_output.  This is
        # intentional: pipeline steps are deterministic, not conditional.
        # If a pipeline should skip remaining steps on NOMINAL, that logic
        # belongs in the pipeline definition, not the generic chainer.
        if result.success and task.payload:
            from genesis.surplus.pipelines import (
                build_next_step_payload,
                get_pipeline,
                is_pipeline_task,
                parse_pipeline_payload,
            )
            if is_pipeline_task(task.payload):
                try:
                    meta = parse_pipeline_payload(task.payload)
                    step = meta.get("step", 1)
                    total = meta.get("total_steps", 1)
                    pipeline_name = meta.get("pipeline", "")
                    if step < total:
                        defn = get_pipeline(pipeline_name)
                        if defn and step < len(defn.steps):
                            next_step = defn.steps[step]  # 0-indexed, step is 1-based
                            next_payload = build_next_step_payload(
                                meta, result.content or "",
                            )
                            await self._queue.enqueue(
                                next_step.task_type,
                                next_step.compute_tier,
                                next_step.priority,
                                defn.drive_alignment,
                                payload=next_payload,
                            )
                            logger.info(
                                "Pipeline %s: step %d/%d complete, enqueued step %d",
                                pipeline_name, step, total, step + 1,
                            )
                        else:
                            logger.warning(
                                "Pipeline %s: step %d references missing definition",
                                pipeline_name, step + 1,
                            )
                    else:
                        logger.info(
                            "Pipeline %s: final step %d/%d complete",
                            pipeline_name, step, total,
                        )
                except Exception:
                    logger.error("Pipeline chaining failed", exc_info=True)

        # Bridge code audit findings to recon observations
        if task.task_type == _TT.CODE_AUDIT and result.insights:
            try:
                from genesis.runtime import GenesisRuntime
                rt = GenesisRuntime.instance()
                if hasattr(rt, '_findings_bridge') and rt._findings_bridge is not None:
                    bridged = await rt._findings_bridge.bridge_findings(result.insights)
                    logger.info("Bridged %d code audit findings to observations", bridged)
            except Exception:
                logger.error("Failed to bridge code audit findings", exc_info=True)

        # Write to brainstorm_log for brainstorm-type tasks
        if task.task_type in (_TT.BRAINSTORM_USER, _TT.BRAINSTORM_SELF):
            try:
                import uuid

                from genesis.db.crud import brainstorm as brainstorm_crud

                session_type = {
                    _TT.BRAINSTORM_USER: "upgrade_user",
                    _TT.BRAINSTORM_SELF: "upgrade_self",
                }.get(task.task_type, str(task.task_type))
                model_used = "unknown"
                if result.insights:
                    model_used = result.insights[0].get("generating_model", "unknown")
                await brainstorm_crud.create(
                    self._db,
                    id=str(uuid.uuid4()),
                    session_type=session_type,
                    model_used=model_used,
                    outputs=result.insights or [],
                    staging_ids=[staging_id] if staging_id else [],
                    created_at=self._clock().isoformat(),
                )
            except Exception:
                logger.error("Failed to write brainstorm_log entry", exc_info=True)

        # Signal autonomy calibration for background cognitive work
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            mgr = getattr(rt, "_autonomy_manager", None)
            if mgr is not None:
                await mgr.record_success("background_cognitive")
        except Exception:
            logger.debug("Autonomy success signal failed (non-fatal)", exc_info=True)

        logger.info("Surplus task %s completed (staging=%s)", task.id, staging_id)
        return True

    async def _maybe_observe_failure(self, task, reason: str) -> None:
        """Create an observation if a task type has 3+ consecutive failures."""
        try:
            from genesis.db.crud import observations, surplus_tasks

            count = await surplus_tasks.consecutive_failures(
                self._db, str(task.task_type),
            )
            if count >= 3:
                obs_id = f"surplus_failing_{task.task_type}"
                await observations.upsert(
                    self._db,
                    id=obs_id,
                    source="surplus_monitor",
                    type="surplus_task_failing",
                    content=(
                        f"Surplus task {task.task_type} has failed "
                        f"{count} consecutive times. Last reason: {reason}"
                    ),
                    priority="high",
                    category="infrastructure",
                    created_at=self._clock().isoformat(),
                )
                logger.warning(
                    "Surplus task %s: %d consecutive failures, observation created",
                    task.task_type, count,
                )
        except Exception:
            logger.debug("Failed to create failure observation", exc_info=True)

    async def _dispatch_loop(self) -> None:
        """Scheduled dispatch callback."""
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Surplus dispatch skipped (Genesis paused)")
                return
        except Exception:
            pass
        try:
            # Record heartbeat at loop entry so the watchdog sees liveness
            # even when a single dispatch_once() blocks for 15-30 minutes
            # (sequential task execution + intake pipeline overhead).
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("surplus_dispatch")
            except Exception:
                pass

            for _ in range(3):
                # Refresh heartbeat before each dispatch so slow or
                # failing tasks don't trip the 900s watchdog threshold.
                with contextlib.suppress(Exception):
                    GenesisRuntime.instance().record_job_success("surplus_dispatch")
                if not await self.dispatch_once():
                    break

            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.SURPLUS, Severity.DEBUG,
                    "heartbeat", "surplus_scheduler dispatch completed",
                )
        except Exception as exc:
            logger.exception("Surplus dispatch loop failed")
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.SURPLUS, Severity.ERROR,
                    "dispatch.failed",
                    "Surplus dispatch loop failed with exception",
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("surplus_dispatch", str(exc))
            except Exception:
                pass

    # GROUNDWORK(v4-parallel-dispatch): dispatch multiple tasks concurrently
