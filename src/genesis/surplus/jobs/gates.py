"""Enqueue-gate jobs — cooldown-gated surplus task enqueues.

Bodies extracted from ``SurplusScheduler``; the scheduler keeps every original
method name as a thin delegate. The nine uniform gate jobs carry their
try/except + job-health protocol via ``@job_guard`` (see ``_guard.py``);
``brainstorm_check`` keeps its pause check and failure event in the body.
Function-scope imports are intentional — they are both the tests'
patch-target seam and the import-cycle breaker; do not hoist them to module
top. Bodies call back through the scheduler instance
(``sched._recently_completed`` / ``sched.schedule_pipeline``) so
instance-level patching in tests keeps working.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.db.crud import surplus as surplus_crud
from genesis.observability.types import Severity, Subsystem
from genesis.surplus.jobs._guard import (
    job_guard,
    record_failure,
    record_success,
)

if TYPE_CHECKING:
    import aiosqlite

    from genesis.surplus.jobs.context import SchedulerContext

logger = logging.getLogger(__name__)


async def brainstorm_check(sched: SchedulerContext) -> None:
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
        await sched._brainstorm_runner.schedule_daily_brainstorms()
        record_success("surplus_brainstorm")
    except Exception as exc:
        logger.exception("Brainstorm check failed")
        record_failure("surplus_brainstorm", str(exc))
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.SURPLUS, Severity.ERROR,
                "brainstorm.failed",
                "Brainstorm check failed with exception",
            )


async def recently_completed(
    sched: SchedulerContext, task_type, cooldown_hours: int | float,
) -> bool:
    """Return ``True`` if *task_type* completed within *cooldown_hours*.

    Used on startup to avoid re-enqueuing tasks that already ran
    recently — prevents Telegram flooding after server restarts.
    """
    last = await sched._queue.last_completed_at(task_type)
    if last is None:
        return False
    try:
        completed = datetime.fromisoformat(last)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=UTC)
        age_s = (sched._clock() - completed).total_seconds()
        return age_s < cooldown_hours * 3600
    except (ValueError, TypeError):
        return False


@job_guard("schedule_code_index", "Code index scheduling failed")
async def schedule_code_index(sched: SchedulerContext) -> None:
    """Enqueue a code index task if none pending/running."""
    from genesis.surplus.types import ComputeTier, TaskType

    active = await sched._queue.active_by_type(TaskType.CODE_INDEX)
    if active == 0 and not await sched._recently_completed(
        TaskType.CODE_INDEX, sched._code_index_hours,
    ):
        await sched._queue.enqueue(
            TaskType.CODE_INDEX, ComputeTier.FREE_API, 0.6, "competence"
        )


@job_guard("schedule_j9_eval_batch", "J9 eval batch scheduling failed")
async def schedule_j9_eval_batch(sched: SchedulerContext) -> None:
    """Enqueue a J9 eval batch task if none pending/running."""
    from genesis.surplus.types import ComputeTier, TaskType

    active = await sched._queue.active_by_type(TaskType.J9_EVAL_BATCH)
    if active == 0 and not await sched._recently_completed(
        TaskType.J9_EVAL_BATCH, sched._j9_eval_batch_hours,
    ):
        await sched._queue.enqueue(
            TaskType.J9_EVAL_BATCH, ComputeTier.FREE_API, 0.3, "competence"
        )


@job_guard("schedule_fresh_session_test", "Fresh session test scheduling failed")
async def schedule_fresh_session_test(sched: SchedulerContext) -> None:
    """Enqueue a FRESH_SESSION_TEST task if none pending/running."""
    from genesis.surplus.types import ComputeTier, TaskType

    active = await sched._queue.active_by_type(TaskType.FRESH_SESSION_TEST)
    if active == 0:
        await sched._queue.enqueue(
            TaskType.FRESH_SESSION_TEST, ComputeTier.FREE_API, 0.2, "competence"
        )


@job_guard("schedule_model_eval", "Model eval scheduling failed")
async def schedule_model_eval(sched: SchedulerContext) -> None:
    """Enqueue a MODEL_EVAL task if none pending/running."""
    import json

    from genesis.surplus.types import ComputeTier, TaskType

    active = await sched._queue.active_by_type(TaskType.MODEL_EVAL)
    if active == 0 and not await sched._recently_completed(
        TaskType.MODEL_EVAL, sched._model_eval_hours,
    ):
        payload = json.dumps({"model_id": "groq-free"})
        await sched._queue.enqueue(
            TaskType.MODEL_EVAL, ComputeTier.FREE_API, 0.4, "competence",
            payload=payload,
        )


async def _run_maintenance_gc(db: aiosqlite.Connection) -> None:
    """GC operations — each wrapped individually so one failure doesn't skip the rest."""
    # Purge expired surplus insights (TTL enforcement)
    try:
        purged = await surplus_crud.purge_expired(db)
        if purged:
            logger.info("Purged %d expired surplus insights", purged)
    except Exception:
        logger.warning("GC: surplus insights purge failed", exc_info=True)

    # GC: remove completed/failed pending_embeddings older than 30 days
    try:
        from genesis.db.crud import pending_embeddings as pe_crud
        pe_purged = await pe_crud.purge_completed(db, older_than_days=30)
        if pe_purged:
            logger.info("Purged %d completed pending_embeddings", pe_purged)
    except Exception:
        logger.warning("GC: pending_embeddings purge failed", exc_info=True)

    # GC: rotate heartbeat events older than 7 days
    try:
        from genesis.db.crud import events as events_crud
        hb_cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        hb_purged = await events_crud.prune(
            db, older_than=hb_cutoff, event_type="heartbeat",
        )
        if hb_purged:
            logger.info("Pruned %d heartbeat events older than 7d", hb_purged)
    except Exception:
        logger.warning("GC: heartbeat event rotation failed", exc_info=True)

    # GC: prune weak memory links (strength <= 0.3, older than 30d)
    try:
        from genesis.db.crud import memory_links as links_crud
        links_pruned = await links_crud.prune_weak(
            db, max_strength=0.3, min_age_days=30,
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


@job_guard("schedule_maintenance", "Maintenance scheduling failed")
async def schedule_maintenance(sched: SchedulerContext) -> None:
    """Enqueue mechanical infrastructure maintenance tasks if none active."""
    from genesis.surplus.types import ComputeTier, TaskType

    # Mechanical tasks only — no LLM needed, run every maintenance_hours
    maintenance_tasks = [
        (TaskType.DISK_CLEANUP, 0.4, "preservation"),
        (TaskType.BACKUP_VERIFICATION, 0.7, "preservation"),
        (TaskType.DEAD_LETTER_REPLAY, 0.5, "cooperation"),
        (TaskType.DB_MAINTENANCE, 0.3, "competence"),
    ]
    for task_type, priority, drive in maintenance_tasks:
        active = await sched._queue.active_by_type(task_type)
        if active == 0 and not await sched._recently_completed(
            task_type, sched._maintenance_hours,
        ):
            await sched._queue.enqueue(
                task_type, ComputeTier.FREE_API, priority, drive,
            )

    # ── GC operations ──────────────────────────────────────────
    from genesis.runtime import GenesisRuntime
    rt = GenesisRuntime.instance()
    if rt.db is not None:
        await _run_maintenance_gc(rt.db)


@job_guard("schedule_analytical", "Analytical scheduling failed")
async def schedule_analytical(sched: SchedulerContext) -> None:
    """Enqueue LLM-based analytical tasks if none active.

    These run on a separate (longer) cadence than mechanical maintenance
    because their inputs change slowly and their free-tier model output
    needs time to be consumed by deep reflection.
    """
    from genesis.surplus.types import ComputeTier, TaskType

    analytical_tasks = [
        (TaskType.GAP_CLUSTERING, 0.4, "competence"),
        # anticipatory_research returns as a pipeline — see pipelines.py.
    ]
    for task_type, priority, drive in analytical_tasks:
        active = await sched._queue.active_by_type(task_type)
        if active == 0 and not await sched._recently_completed(
            task_type, sched._analytical_hours,
        ):
            await sched._queue.enqueue(
                task_type, ComputeTier.FREE_API, priority, drive,
            )
    # prompt_effectiveness runs as a 3-step pipeline.
    await sched.schedule_pipeline("prompt_effectiveness")
    await sched.schedule_pipeline("anticipatory_research")


@job_guard("schedule_wing_audit", "Wing audit scheduling failed")
async def schedule_wing_audit(sched: SchedulerContext) -> None:
    """Enqueue a wing audit task if none pending/running."""
    from genesis.surplus.types import ComputeTier, TaskType

    active = await sched._queue.active_by_type(TaskType.WING_AUDIT)
    if active == 0:
        await sched._queue.enqueue(
            TaskType.WING_AUDIT, ComputeTier.FREE_API, 0.4, "competence"
        )


@job_guard("schedule_cc_memory_staleness", "CC memory staleness scheduling failed")
async def schedule_cc_memory_staleness(sched: SchedulerContext) -> None:
    """Enqueue a CC memory staleness scan if none pending/running."""
    from genesis.surplus.types import ComputeTier, TaskType

    active = await sched._queue.active_by_type(TaskType.CC_MEMORY_STALENESS)
    if active == 0:
        await sched._queue.enqueue(
            TaskType.CC_MEMORY_STALENESS, ComputeTier.FREE_API, 0.3, "competence"
        )


async def schedule_pipeline(sched: SchedulerContext, pipeline_name: str) -> str | None:
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
    if await sched._queue.active_by_type(step1.task_type) > 0:
        return None

    # Cooldown: skip if the pipeline's final step completed recently.
    # Uses the last step because that's when the full pipeline finished.
    last_step = defn.steps[-1]
    if await sched._recently_completed(
        last_step.task_type, sched._analytical_hours,
    ):
        return None

    payload = build_initial_payload(pipeline_name, len(defn.steps))
    task_id = await sched._queue.enqueue(
        step1.task_type,
        step1.compute_tier,
        step1.priority,
        defn.drive_alignment,
        payload=payload,
    )
    logger.info("Pipeline %s: enqueued step 1 (task=%s)", pipeline_name, task_id[:8])
    return task_id
