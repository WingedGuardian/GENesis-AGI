"""SurplusScheduler — orchestrates surplus compute dispatch with own APScheduler."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from genesis.db.crud import surplus as surplus_crud
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.surplus.brainstorm import BrainstormRunner
from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.executor import StubExecutor
from genesis.surplus.idle_detector import IdleDetector
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.types import SurplusExecutor

if TYPE_CHECKING:
    from genesis.memory.store import MemoryStore
    from genesis.recon.gatherer import ReconGatherer
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


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
        self._clock = clock or (lambda: datetime.now(UTC))
        self._code_audit_executor: SurplusExecutor | None = None
        self._code_index_executor: SurplusExecutor | None = None
        self._bookmark_enrichment_executor: SurplusExecutor | None = None
        self._model_eval_executor: SurplusExecutor | None = None
        self._disk_cleanup_executor: SurplusExecutor | None = None
        self._backup_verification_executor: SurplusExecutor | None = None
        self._dead_letter_replay_executor: SurplusExecutor | None = None
        self._db_maintenance_executor: SurplusExecutor | None = None
        self._recon_gatherer: ReconGatherer | None = None
        self._extraction_store: MemoryStore | None = None
        self._extraction_router: Router | None = None
        self._follow_up_dispatcher = None  # Set via set_follow_up_dispatcher()
        self._scheduler = AsyncIOScheduler()

    def set_executor(self, executor) -> None:
        """Replace the current executor (e.g., swap StubExecutor for a real one)."""
        self._executor = executor
        self._brainstorm_runner._executor = executor

    def set_code_audit_executor(self, executor) -> None:
        """Set a dedicated executor for CODE_AUDIT tasks."""
        self._code_audit_executor = executor

    def set_code_index_executor(self, executor: SurplusExecutor) -> None:
        """Set a dedicated executor for CODE_INDEX tasks (no LLM, pure AST)."""
        self._code_index_executor = executor

    def set_bookmark_enrichment_executor(self, executor: SurplusExecutor) -> None:
        """Set a dedicated executor for BOOKMARK_ENRICHMENT tasks."""
        self._bookmark_enrichment_executor = executor

    def set_model_eval_executor(self, executor: SurplusExecutor) -> None:
        """Set a dedicated executor for MODEL_EVAL tasks."""
        self._model_eval_executor = executor

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
            self._disk_cleanup_executor = disk_cleanup
        if backup_verification:
            self._backup_verification_executor = backup_verification
        if dead_letter_replay:
            self._dead_letter_replay_executor = dead_letter_replay
        if db_maintenance:
            self._db_maintenance_executor = db_maintenance

    def set_recon_gatherer(self, gatherer: ReconGatherer) -> None:
        """Set the recon gatherer for scheduled release checking."""
        self._recon_gatherer = gatherer

    def set_extraction_deps(
        self,
        *,
        store: MemoryStore,
        router: Router,
    ) -> None:
        """Set dependencies for the memory extraction job."""
        self._extraction_store = store
        self._extraction_router = router

    def set_follow_up_dispatcher(self, dispatcher) -> None:
        """Set the follow-up dispatcher for accountability tracking.

        Registers the dispatch job if the scheduler is already running.
        """
        self._follow_up_dispatcher = dispatcher
        # Register the job if the scheduler is already running (late wiring)
        if self._scheduler.running and not self._scheduler.get_job("follow_up_dispatch"):
            self._scheduler.add_job(
                self.dispatch_follow_ups,
                IntervalTrigger(minutes=self._dispatch_interval),
                id="follow_up_dispatch",
                max_instances=1,
                misfire_grace_time=60,
            )

    async def start(self) -> None:
        """Start the surplus scheduler with brainstorm check and dispatch jobs."""
        self._scheduler.add_job(
            self.brainstorm_check,
            IntervalTrigger(hours=self._brainstorm_interval),
            id="surplus_brainstorm_check",
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.add_job(
            self._dispatch_loop,
            IntervalTrigger(minutes=self._dispatch_interval),
            id="surplus_dispatch",
            max_instances=1,
            misfire_grace_time=60,
        )
        if self._enable_code_audits:
            self._scheduler.add_job(
                self.schedule_code_audit,
                IntervalTrigger(hours=12),
                id="schedule_code_audit",
                max_instances=1,
                misfire_grace_time=300,
                next_run_time=datetime.now(UTC) + timedelta(seconds=60),
            )
        else:
            logger.info("Code audits disabled — skipping job registration")
        self._scheduler.add_job(
            self.schedule_code_index,
            IntervalTrigger(hours=4),
            id="schedule_code_index",
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.add_job(
            self.schedule_infra_monitor,
            IntervalTrigger(hours=2),
            id="schedule_infra_monitor",
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.add_job(
            self.run_recon_gather,
            IntervalTrigger(hours=84),
            id="recon_gather",
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.add_job(
            self.schedule_maintenance,
            IntervalTrigger(hours=6),
            id="schedule_maintenance",
            max_instances=1,
            misfire_grace_time=300,
        )
        if self._follow_up_dispatcher is not None:
            self._scheduler.add_job(
                self.dispatch_follow_ups,
                IntervalTrigger(minutes=self._dispatch_interval),
                id="follow_up_dispatch",
                max_instances=1,
                misfire_grace_time=60,
            )
        self._scheduler.start()
        # Run brainstorm check immediately on startup
        await self.brainstorm_check()
        # Also run infra monitor and recon gather immediately —
        # otherwise they only fire after their IntervalTrigger elapses.
        if self._enable_code_audits:
            await self.schedule_code_audit()
        await self.schedule_code_index()
        await self.schedule_infra_monitor()
        await self.run_recon_gather()
        await self.schedule_maintenance()
        logger.info(
            "Surplus scheduler started (dispatch=%dm, brainstorm=%dh)",
            self._dispatch_interval, self._brainstorm_interval,
        )

    async def stop(self) -> None:
        """Stop the scheduler, waiting for any running job to finish."""
        self._scheduler.shutdown(wait=True)
        logger.info("Surplus scheduler stopped")

    async def brainstorm_check(self) -> None:
        """Ensure today's brainstorm sessions are queued."""
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Brainstorm check skipped (Genesis paused)")
                return
        except Exception:
            logger.warning("Pause check failed — skipping brainstorm as precaution", exc_info=True)
            return
        try:
            await self._brainstorm_runner.schedule_daily_brainstorms()
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("surplus_brainstorm")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Brainstorm check failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("surplus_brainstorm", str(exc))
            except Exception:
                pass
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.SURPLUS, Severity.ERROR,
                    "brainstorm.failed",
                    "Brainstorm check failed with exception",
                )

    async def schedule_code_audit(self) -> None:
        """Enqueue a code audit task if none pending/running."""
        if not self._enable_code_audits:
            return
        try:
            from genesis.surplus.types import ComputeTier, TaskType

            pending = await self._queue.pending_by_type(TaskType.CODE_AUDIT)
            if pending == 0:
                await self._queue.enqueue(
                    TaskType.CODE_AUDIT, ComputeTier.FREE_API, 0.5, "competence"
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("schedule_code_audit")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Code audit scheduling failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("schedule_code_audit", str(exc))
            except Exception:
                pass

    async def schedule_code_index(self) -> None:
        """Enqueue a code index task if none pending/running."""
        try:
            from genesis.surplus.types import ComputeTier, TaskType

            pending = await self._queue.pending_by_type(TaskType.CODE_INDEX)
            if pending == 0:
                await self._queue.enqueue(
                    TaskType.CODE_INDEX, ComputeTier.LOCAL_30B, 0.6, "competence"
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("schedule_code_index")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Code index scheduling failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("schedule_code_index", str(exc))
            except Exception:
                pass

    async def schedule_infra_monitor(self) -> None:
        """Enqueue an infrastructure monitor task if none pending/running."""
        try:
            from genesis.surplus.types import ComputeTier, TaskType

            pending = await self._queue.pending_by_type(TaskType.INFRASTRUCTURE_MONITOR)
            if pending == 0:
                await self._queue.enqueue(
                    TaskType.INFRASTRUCTURE_MONITOR, ComputeTier.FREE_API, 0.6, "preservation"
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("schedule_infra_monitor")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Infrastructure monitor scheduling failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("schedule_infra_monitor", str(exc))
            except Exception:
                pass

    async def schedule_maintenance(self) -> None:
        """Enqueue infrastructure maintenance tasks if none pending."""
        try:
            from genesis.surplus.types import ComputeTier, TaskType

            # Each task: check pending, enqueue if zero
            # Infrastructure maintenance (no LLM needed)
            maintenance_tasks = [
                (TaskType.DISK_CLEANUP, 0.4, "preservation"),
                (TaskType.BACKUP_VERIFICATION, 0.7, "preservation"),
                (TaskType.DEAD_LETTER_REPLAY, 0.5, "cooperation"),
                (TaskType.DB_MAINTENANCE, 0.3, "competence"),
                # Tier 1 LLM tasks — observation-only analysis via ReflectionEngine
                (TaskType.GAP_CLUSTERING, 0.4, "competence"),
                (TaskType.ANTICIPATORY_RESEARCH, 0.3, "curiosity"),
                (TaskType.PROMPT_EFFECTIVENESS_REVIEW, 0.3, "competence"),
            ]
            for task_type, priority, drive in maintenance_tasks:
                pending = await self._queue.pending_by_type(task_type)
                if pending == 0:
                    await self._queue.enqueue(
                        task_type, ComputeTier.FREE_API, priority, drive,
                    )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("schedule_maintenance")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Maintenance scheduling failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("schedule_maintenance", str(exc))
            except Exception:
                pass

    async def dispatch_follow_ups(self) -> None:
        """Run the follow-up dispatcher cycle (always-on, not idle-gated)."""
        if self._follow_up_dispatcher is None:
            return
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Follow-up dispatch skipped (Genesis paused)")
                return
        except Exception:
            pass
        try:
            summary = await self._follow_up_dispatcher.run_cycle()
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("follow_up_dispatch")
            except Exception:
                pass
            if summary.get("failures_detected", 0) > 0:
                logger.warning(
                    "Follow-up dispatch detected %d failure(s)",
                    summary["failures_detected"],
                )
        except Exception as exc:
            logger.exception("Follow-up dispatch failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("follow_up_dispatch", str(exc))
            except Exception:
                pass

    async def run_recon_gather(self) -> None:
        """Check watchlist projects for new GitHub releases."""
        if self._recon_gatherer is None:
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("recon_gather", "gatherer not wired")
            except Exception:
                pass
            return
        try:
            result = await self._recon_gatherer.gather_releases()
            if result.new_findings > 0:
                logger.info(
                    "Recon gather found %d new release(s): %s",
                    result.new_findings, "; ".join(result.details),
                )
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.RECON, Severity.DEBUG,
                    "heartbeat", "recon_gather completed",
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("recon_gather")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Recon gather failed")
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.RECON, Severity.ERROR,
                    "recon_gather.failed",
                    "Recon gather failed with exception",
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("recon_gather", str(exc))
            except Exception:
                pass

    async def run_memory_extraction(self) -> None:
        """Run periodic memory extraction from session transcripts."""
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Memory extraction skipped (Genesis paused)")
                return
        except Exception:
            logger.warning("Pause check failed — skipping extraction as precaution", exc_info=True)
            return
        if self._extraction_store is None or self._extraction_router is None:
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure(
                    "memory_extraction", "extraction deps not wired",
                )
            except Exception:
                pass
            return
        try:
            from genesis.memory.extraction_job import run_extraction_cycle

            # Get linker from store for typed link creation
            linker = self._extraction_store.linker
            summary = await run_extraction_cycle(
                db=self._db,
                store=self._extraction_store,
                router=self._extraction_router,
                linker=linker,
            )
            logger.info(
                "Memory extraction completed: %d sessions, %d entities, %d errors",
                summary["sessions_processed"],
                summary["entities_extracted"],
                summary["errors"],
            )
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.SURPLUS, Severity.DEBUG,
                    "heartbeat", "memory_extraction completed",
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("memory_extraction")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Memory extraction failed")
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.SURPLUS, Severity.ERROR,
                    "memory_extraction.failed",
                    "Memory extraction failed with exception",
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("memory_extraction", str(exc))
            except Exception:
                pass

    async def dispatch_once(self) -> bool:
        """Single dispatch cycle. Returns True if a task was processed."""
        # 0. Recover tasks stuck in 'running' state (crashed mid-execution)
        await self._queue.recover_stuck()

        # 1. Drain expired tasks
        await self._queue.drain_expired(max_age_hours=self._task_expiry_hours)

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

        executor = self._executor
        if task.task_type == _TT.CODE_AUDIT and self._code_audit_executor is not None:
            executor = self._code_audit_executor
        elif task.task_type == _TT.CODE_INDEX and self._code_index_executor is not None:
            executor = self._code_index_executor
        elif task.task_type == _TT.BOOKMARK_ENRICHMENT and self._bookmark_enrichment_executor is not None:
            executor = self._bookmark_enrichment_executor
        elif task.task_type == _TT.MODEL_EVAL and self._model_eval_executor is not None:
            executor = self._model_eval_executor
        elif task.task_type == _TT.DISK_CLEANUP and self._disk_cleanup_executor is not None:
            executor = self._disk_cleanup_executor
        elif task.task_type == _TT.BACKUP_VERIFICATION and self._backup_verification_executor is not None:
            executor = self._backup_verification_executor
        elif task.task_type == _TT.DEAD_LETTER_REPLAY and self._dead_letter_replay_executor is not None:
            executor = self._dead_letter_replay_executor
        elif task.task_type == _TT.DB_MAINTENANCE and self._db_maintenance_executor is not None:
            executor = self._db_maintenance_executor

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
            return False

        # 6. Write to staging (with content-hash dedup + quality gate)
        staging_id = None
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

        await self._queue.mark_completed(task.id, staging_id=staging_id)

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
            for _ in range(3):
                if not await self.dispatch_once():
                    break
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.SURPLUS, Severity.DEBUG,
                    "heartbeat", "surplus_scheduler dispatch completed",
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("surplus_dispatch")
            except Exception:
                pass
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
