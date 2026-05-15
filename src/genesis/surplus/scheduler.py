"""SurplusScheduler — orchestrates surplus compute dispatch with own APScheduler."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
        code_audit_hours: int = 12,
        code_index_hours: int = 4,
        recon_gather_hours: int = 84,
        maintenance_hours: int = 6,
        analytical_hours: int = 24,
        follow_up_dispatch_minutes: int | None = None,
        memory_extraction_hours: int = 2,
        j9_eval_batch_hours: int = 24,
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
        self._code_audit_hours = code_audit_hours
        self._code_index_hours = code_index_hours
        self._recon_gather_hours = recon_gather_hours
        self._maintenance_hours = maintenance_hours
        self._analytical_hours = analytical_hours
        self._follow_up_dispatch_minutes = follow_up_dispatch_minutes or dispatch_interval_minutes
        self._memory_extraction_hours = memory_extraction_hours
        self._j9_eval_batch_hours = j9_eval_batch_hours
        self._clock = clock or (lambda: datetime.now(UTC))
        self._code_audit_executor: SurplusExecutor | None = None
        self._code_index_executor: SurplusExecutor | None = None
        self._bookmark_enrichment_executor: SurplusExecutor | None = None
        self._model_eval_executor: SurplusExecutor | None = None
        self._j9_eval_batch_executor: SurplusExecutor | None = None
        self._disk_cleanup_executor: SurplusExecutor | None = None
        self._backup_verification_executor: SurplusExecutor | None = None
        self._dead_letter_replay_executor: SurplusExecutor | None = None
        self._db_maintenance_executor: SurplusExecutor | None = None
        self._recon_gatherer: ReconGatherer | None = None
        self._model_intelligence_job = None  # Set via set_model_intelligence_job()
        self._extraction_store: MemoryStore | None = None
        self._extraction_router: Router | None = None
        self._follow_up_dispatcher = None  # Set via set_follow_up_dispatcher()
        self._topic_manager = None
        self._scheduler = AsyncIOScheduler()

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

    def set_j9_eval_batch_executor(self, executor: SurplusExecutor) -> None:
        """Set executor for J9_EVAL_BATCH tasks (daily memory relevance scoring)."""
        self._j9_eval_batch_executor = executor

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

    def set_model_intelligence_job(self, job) -> None:
        """Set the ModelIntelligenceJob for scheduled model landscape scanning."""
        self._model_intelligence_job = job

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
                IntervalTrigger(minutes=self._follow_up_dispatch_minutes),
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
                IntervalTrigger(hours=self._code_audit_hours),
                id="schedule_code_audit",
                max_instances=1,
                misfire_grace_time=300,
                next_run_time=datetime.now(UTC) + timedelta(seconds=60),
            )
        else:
            logger.info("Code audits disabled — skipping job registration")
        self._scheduler.add_job(
            self.schedule_code_index,
            IntervalTrigger(hours=self._code_index_hours),
            id="schedule_code_index",
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.add_job(
            self.run_recon_gather,
            IntervalTrigger(hours=self._recon_gather_hours),
            id="recon_gather",
            max_instances=1,
            misfire_grace_time=300,
        )
        # Model intelligence: weekly Sunday 6am (per config/recon_schedules.yaml)
        if self._model_intelligence_job is not None:
            from apscheduler.triggers.cron import CronTrigger
            self._scheduler.add_job(
                self.run_model_intelligence,
                CronTrigger(day_of_week="sun", hour=6),
                id="model_intelligence",
                max_instances=1,
                misfire_grace_time=3600,
            )
        self._scheduler.add_job(
            self.schedule_maintenance,
            IntervalTrigger(hours=self._maintenance_hours),
            id="schedule_maintenance",
            max_instances=1,
            misfire_grace_time=300,
        )
        if self._analytical_hours > 0:
            self._scheduler.add_job(
                self.schedule_analytical,
                IntervalTrigger(hours=self._analytical_hours),
                id="schedule_analytical",
                max_instances=1,
                misfire_grace_time=300,
            )
        if self._j9_eval_batch_hours > 0:
            self._scheduler.add_job(
                self.schedule_j9_eval_batch,
                IntervalTrigger(hours=self._j9_eval_batch_hours),
                id="schedule_j9_eval_batch",
                max_instances=1,
                misfire_grace_time=300,
            )
        if self._follow_up_dispatcher is not None:
            self._scheduler.add_job(
                self.dispatch_follow_ups,
                IntervalTrigger(minutes=self._follow_up_dispatch_minutes),
                id="follow_up_dispatch",
                max_instances=1,
                misfire_grace_time=60,
            )
        self._scheduler.start()
        # Run brainstorm check immediately on startup
        await self.brainstorm_check()
        # Run remaining jobs immediately on startup —
        # otherwise they only fire after their IntervalTrigger elapses.
        if self._enable_code_audits:
            await self.schedule_code_audit()
        await self.schedule_code_index()
        await self.run_recon_gather()
        await self.schedule_maintenance()
        if self._j9_eval_batch_hours > 0:
            await self.schedule_j9_eval_batch()
        if self._analytical_hours > 0:
            await self.schedule_analytical()
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

            active = await self._queue.active_by_type(TaskType.CODE_AUDIT)
            if active == 0:
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

            active = await self._queue.active_by_type(TaskType.CODE_INDEX)
            if active == 0:
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

    async def schedule_j9_eval_batch(self) -> None:
        """Enqueue a J9 eval batch task if none pending/running."""
        try:
            from genesis.surplus.types import ComputeTier, TaskType

            active = await self._queue.active_by_type(TaskType.J9_EVAL_BATCH)
            if active == 0:
                await self._queue.enqueue(
                    TaskType.J9_EVAL_BATCH, ComputeTier.FREE_API, 0.3, "competence"
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("schedule_j9_eval_batch")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("J9 eval batch scheduling failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("schedule_j9_eval_batch", str(exc))
            except Exception:
                pass

    async def schedule_maintenance(self) -> None:
        """Enqueue mechanical infrastructure maintenance tasks if none active."""
        try:
            from genesis.surplus.types import ComputeTier, TaskType

            # Mechanical tasks only — no LLM needed, run every maintenance_hours
            maintenance_tasks = [
                (TaskType.DISK_CLEANUP, 0.4, "preservation"),
                (TaskType.BACKUP_VERIFICATION, 0.7, "preservation"),
                (TaskType.DEAD_LETTER_REPLAY, 0.5, "cooperation"),
                (TaskType.DB_MAINTENANCE, 0.3, "competence"),
            ]
            for task_type, priority, drive in maintenance_tasks:
                active = await self._queue.active_by_type(task_type)
                if active == 0:
                    await self._queue.enqueue(
                        task_type, ComputeTier.FREE_API, priority, drive,
                    )

            # ── GC operations ──────────────────────────────────────────
            # Each wrapped individually so one failure doesn't skip the rest.
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            if rt.db is not None:
                # Purge expired surplus insights (TTL enforcement)
                try:
                    purged = await surplus_crud.purge_expired(rt.db)
                    if purged:
                        logger.info("Purged %d expired surplus insights", purged)
                except Exception:
                    logger.warning("GC: surplus insights purge failed", exc_info=True)

                # GC: remove completed/failed pending_embeddings older than 30 days
                try:
                    from genesis.db.crud import pending_embeddings as pe_crud
                    pe_purged = await pe_crud.purge_completed(rt.db, older_than_days=30)
                    if pe_purged:
                        logger.info("Purged %d completed pending_embeddings", pe_purged)
                except Exception:
                    logger.warning("GC: pending_embeddings purge failed", exc_info=True)

                # GC: rotate heartbeat events older than 7 days
                try:
                    from genesis.db.crud import events as events_crud
                    hb_cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
                    hb_purged = await events_crud.prune(
                        rt.db, older_than=hb_cutoff, event_type="heartbeat",
                    )
                    if hb_purged:
                        logger.info("Pruned %d heartbeat events older than 7d", hb_purged)
                except Exception:
                    logger.warning("GC: heartbeat event rotation failed", exc_info=True)

                # GC: prune weak memory links (strength <= 0.3, older than 30d)
                try:
                    from genesis.db.crud import memory_links as links_crud
                    links_pruned = await links_crud.prune_weak(
                        rt.db, max_strength=0.3, min_age_days=30,
                    )
                    if links_pruned:
                        logger.info("Pruned %d weak memory links", links_pruned)
                except Exception:
                    logger.warning("GC: weak link pruning failed", exc_info=True)

                # GC: archive old transcript files (gzip .jsonl > 90 days)
                try:
                    from genesis.surplus.maintenance import archive_old_transcripts
                    transcripts_archived = await archive_old_transcripts(
                        Path.home() / ".genesis" / "background-sessions",
                        older_than_days=90,
                    )
                    if transcripts_archived:
                        logger.info(
                            "Archived %d old transcript files", transcripts_archived,
                        )
                except Exception:
                    logger.warning("GC: transcript archival failed", exc_info=True)

            with contextlib.suppress(Exception):
                GenesisRuntime.instance().record_job_success("schedule_maintenance")
        except Exception as exc:
            logger.exception("Maintenance scheduling failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("schedule_maintenance", str(exc))
            except Exception:
                pass

    async def schedule_analytical(self) -> None:
        """Enqueue LLM-based analytical tasks if none active.

        These run on a separate (longer) cadence than mechanical maintenance
        because their inputs change slowly and their free-tier model output
        needs time to be consumed by deep reflection.
        """
        try:
            from genesis.surplus.types import ComputeTier, TaskType

            analytical_tasks = [
                (TaskType.GAP_CLUSTERING, 0.4, "competence"),
                # anticipatory_research returns as a pipeline — see pipelines.py.
            ]
            for task_type, priority, drive in analytical_tasks:
                active = await self._queue.active_by_type(task_type)
                if active == 0:
                    await self._queue.enqueue(
                        task_type, ComputeTier.FREE_API, priority, drive,
                    )
            # prompt_effectiveness runs as a 3-step pipeline.
            await self.schedule_pipeline("prompt_effectiveness")
            await self.schedule_pipeline("anticipatory_research")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("schedule_analytical")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Analytical scheduling failed")
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("schedule_analytical", str(exc))
            except Exception:
                pass

    async def schedule_pipeline(self, pipeline_name: str) -> str | None:
        """Enqueue step 1 of a named pipeline if not already running.

        Returns the task ID of the enqueued step, or None if skipped
        (pipeline unknown, or step 1 task type already pending).
        """
        from genesis.surplus.pipelines import build_initial_payload, get_pipeline

        defn = get_pipeline(pipeline_name)
        if defn is None:
            logger.warning("Unknown pipeline: %s", pipeline_name)
            return None

        step1 = defn.steps[0]
        # Prevent re-enqueue if step 1's task type is already pending or
        # running.  Checks RUNNING too — otherwise a slow pipeline step
        # could allow a duplicate enqueue on the next scheduled cycle.
        if await self._queue.active_by_type(step1.task_type) > 0:
            return None

        payload = build_initial_payload(pipeline_name, len(defn.steps))
        task_id = await self._queue.enqueue(
            step1.task_type,
            step1.compute_tier,
            step1.priority,
            defn.drive_alignment,
            payload=payload,
        )
        logger.info("Pipeline %s: enqueued step 1 (task=%s)", pipeline_name, task_id[:8])
        return task_id

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
        """Check watchlist projects for new GitHub releases and star counts."""
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
            try:
                star_result = await self._recon_gatherer.gather_stars()
                if star_result.new_findings > 0:
                    logger.info(
                        "Star gather found %d change(s): %s",
                        star_result.new_findings, "; ".join(star_result.details),
                    )
            except Exception:
                logger.exception("Star gather failed (releases unaffected)")
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

    async def run_model_intelligence(self) -> None:
        """Run model intelligence scan (weekly)."""
        if self._model_intelligence_job is None:
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure(
                    "model_intelligence", "job not wired",
                )
            except Exception:
                pass
            return
        try:
            result = await self._model_intelligence_job.run()
            total = result.get("total_findings", 0)
            logger.info("Model intelligence scan: %d findings", total)
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.RECON, Severity.DEBUG,
                    "heartbeat", "model_intelligence completed",
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_success("model_intelligence")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Model intelligence scan failed")
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.RECON, Severity.ERROR,
                    "model_intelligence.failed",
                    "Model intelligence scan failed",
                )
            try:
                from genesis.runtime import GenesisRuntime
                GenesisRuntime.instance().record_job_failure("model_intelligence", str(exc))
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
        elif task.task_type == _TT.J9_EVAL_BATCH and self._j9_eval_batch_executor is not None:
            executor = self._j9_eval_batch_executor

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

        await self._queue.mark_completed(task.id, staging_id=staging_id)

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
