"""Surplus dispatch engine — the single-cycle task dispatch pipeline.

Extracted from ``SurplusScheduler.dispatch_once`` (which remains on the
scheduler as a facade — tests and ``_dispatch_loop`` call it there). The
pipeline is decomposed into named phases with NO logic change:

    dispatch_once
      ├─ _select_executor            registry lookup + default fallback
      ├─ _handle_failure             shared failure path (see ASYMMETRY note)
      ├─ _route_insights             intake routing + quality judge
      ├─ _chain_pipeline             enqueue next pipeline step
      ├─ _bridge_code_audit_findings findings → recon observations
      ├─ _log_brainstorm             brainstorm_log persistence
      └─ _signal_autonomy_success    autonomy calibration signal

Function-scope imports are intentional — they are the tests' patch-target
seam and the import-cycle breaker; do not hoist them to module top.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from genesis.db.crud import surplus as surplus_crud
from genesis.observability.types import Severity, Subsystem
from genesis.surplus.types import (
    INSIGHT_PRODUCING_TASK_TYPES,
    KB_ROUTING_TASK_TYPES,
    ComputeTier,
    TaskType,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    import aiosqlite

    from genesis.observability.events import GenesisEventBus
    from genesis.routing.router import Router
    from genesis.surplus.compute_availability import ComputeAvailability
    from genesis.surplus.idle_detector import IdleDetector
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.types import SurplusExecutor

logger = logging.getLogger(__name__)

# A single dispatch_once phase (queue maintenance or task selection) is normally
# sub-second. One this slow is abnormal and is the diagnostic surface for the
# 2026-09-01 heartbeat-stall class (a phase blocking with NO task running). Observe
# only — never a timeout: a legitimately long task lives inside `execute`, not these
# phases, so this only LOGS to localize a stall the heavy_workload flag already forgives.
_SLOW_PHASE_WARN_S = 120.0


@contextlib.contextmanager
def _timed_phase(name: str):
    """Time a dispatch_once phase; WARN (never kill) if it runs abnormally long."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        dt = time.monotonic() - t0
        if dt > _SLOW_PHASE_WARN_S:
            logger.warning(
                "surplus dispatch phase '%s' took %.0fs (>%ds) — possible "
                "heartbeat-stall source; heavy_workload forgives the watchdog, this "
                "log localizes the stalling phase",
                name,
                dt,
                int(_SLOW_PHASE_WARN_S),
            )


class DispatchContext(Protocol):
    """The slice of ``SurplusScheduler`` that the dispatch pipeline reads.

    Every read is an attribute lookup on the live scheduler at call time —
    this preserves the staged-init semantics (``set_executor`` deliberately
    swaps ``_executor`` AFTER ``start()``; a snapshot would dispatch to the
    StubExecutor forever).
    """

    _db: aiosqlite.Connection
    _event_bus: GenesisEventBus | None
    _queue: SurplusQueue
    _idle_detector: IdleDetector
    _compute: ComputeAvailability
    _executor: SurplusExecutor
    _executors: dict[TaskType, SurplusExecutor]
    _judge_router: Router | None
    _clock: Callable[[], datetime]
    _task_expiry_hours: int
    _terminal_retention_days: int


def _select_executor(sched: DispatchContext, task) -> SurplusExecutor:
    """Dedicated executor for this task type if registered; the default
    executor otherwise (a registered-but-None entry also falls back,
    matching the old per-slot `is not None` checks)."""
    executor = sched._executors.get(task.task_type)
    if executor is None:
        executor = sched._executor
    return executor


async def _handle_failure(
    sched: DispatchContext, task, reason: str, *, emit_event: bool,
) -> None:
    """Shared failure path: mark failed, optionally emit, signal autonomy,
    maybe observe.

    ASYMMETRY (intentional, preserved from the original inline code): the
    executor-exception path emits a ``task.failed`` event (``emit_event=True``);
    the ``result.success == False`` path does not (``emit_event=False``).
    """
    await sched._queue.mark_failed(task.id, reason=reason)
    if emit_event and sched._event_bus:
        await sched._event_bus.emit(
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
    await maybe_observe_failure(sched, task, reason)


async def _route_insights(
    sched: DispatchContext, task, result,
) -> tuple[str | None, str | None, float | None, str | None]:
    """Step 6 — route through intake pipeline (atomize → score → route to
    knowledge) and run the measurement-only quality judge.

    Returns ``(staging_id, outcome_quality, judge_score, judge_detail)``.
    """
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
            if task.task_type == TaskType.CODE_AUDIT:
                # Code-audit output is ingested per-finding by
                # FindingsBridge below, behind its confidence gate and
                # slop filter. Routing the raw findings array through
                # generic intake as well would double-ingest every
                # finding and bypass both gates. Synthetic staging_id
                # for tracking; the quality judge below still runs.
                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                staging_id = f"{task.task_type.value}-{content_hash}"
            elif task.task_type not in KB_ROUTING_TASK_TYPES:
                # Non-KB-routing task (action/maintenance/monitor/pipeline-
                # intermediate — e.g. DB_MAINTENANCE, DISK_CLEANUP, MODEL_EVAL,
                # J9_EVAL_BATCH): its output is point-in-time OPERATIONAL
                # TELEMETRY (a maintenance report, an eval-batch summary), NOT
                # durable knowledge. Never ingest it into the knowledge base —
                # doing so is what polluted the KB to 71% surplus. The task's
                # outcome is still recorded via the normal outcome-bus path; we
                # keep a synthetic staging_id for tracking only. The quality
                # judge below stays gated on INSIGHT_PRODUCING and no-ops here.
                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                staging_id = f"{task.task_type.value}-{content_hash}"
                logger.debug(
                    "Skipping KB intake for non-KB-routing task %s (%s)",
                    task.task_type.value, task.id[:8],
                )
            else:
                try:
                    from genesis.surplus.intake import (
                        _valid_drive,
                        run_intake,
                        source_for_task_type,
                    )
                    source = source_for_task_type(str(task.task_type))
                    intake_stats = await run_intake(
                        content=content,
                        source=source,
                        source_task_type=str(task.task_type),
                        generating_model=insight.get("generating_model", "unknown"),
                        drive_alignment=task.drive_alignment,
                        db=sched._db,
                    )
                    logger.info(
                        "Intake routed %d findings (k=%d, o=%d, d=%d, staged=%d) "
                        "for task %s",
                        intake_stats.findings_count,
                        intake_stats.routed_knowledge,
                        intake_stats.routed_observation,
                        intake_stats.routed_discard,
                        intake_stats.routed_staging,
                        task.id[:8],
                    )
                    # Prefer the real staged row id (ideation → surplus_insights)
                    # so a linked follow-up's result_staging_id resolves; else a
                    # synthetic tracking id (KB-routed tasks stage no surplus row).
                    if intake_stats.staged_ids:
                        staging_id = intake_stats.staged_ids[0]
                    else:
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
                    now = sched._clock()
                    ttl = (now + timedelta(days=7)).isoformat()
                    now_iso = now.isoformat()
                    await surplus_crud.upsert(
                        sched._db,
                        id=staging_id,
                        content=content,
                        source_task_type=str(task.task_type),
                        generating_model=insight.get("generating_model", "unknown"),
                        drive_alignment=_valid_drive(task.drive_alignment),
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
                        content, task.task_type, sched._judge_router,
                    )
                )
    return staging_id, outcome_quality, judge_score, judge_detail


async def _chain_pipeline(sched: DispatchContext, task, result) -> None:
    """Pipeline chaining — enqueue next step if this was a pipeline task.

    Note: if a step returns NOMINAL (empty content), chaining still
    proceeds — the next step gets empty previous_output.  This is
    intentional: pipeline steps are deterministic, not conditional.
    If a pipeline should skip remaining steps on NOMINAL, that logic
    belongs in the pipeline definition, not the generic chainer.
    """
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
                        await sched._queue.enqueue(
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


async def _bridge_code_audit_findings(task, result) -> None:
    """Bridge code audit findings to recon observations."""
    if task.task_type == TaskType.CODE_AUDIT and result.insights:
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            if hasattr(rt, '_findings_bridge') and rt._findings_bridge is not None:
                bridged = await rt._findings_bridge.bridge_findings(result.insights)
                logger.info("Bridged %d code audit findings to observations", bridged)
        except Exception:
            logger.error("Failed to bridge code audit findings", exc_info=True)


async def _log_brainstorm(
    sched: DispatchContext, task, result, staging_id: str | None,
) -> None:
    """Write to brainstorm_log for brainstorm-type tasks."""
    if task.task_type in (TaskType.BRAINSTORM_USER, TaskType.BRAINSTORM_SELF):
        try:
            import uuid

            from genesis.db.crud import brainstorm as brainstorm_crud

            session_type = {
                TaskType.BRAINSTORM_USER: "upgrade_user",
                TaskType.BRAINSTORM_SELF: "upgrade_self",
            }.get(task.task_type, str(task.task_type))
            model_used = "unknown"
            if result.insights:
                model_used = result.insights[0].get("generating_model", "unknown")
            await brainstorm_crud.create(
                sched._db,
                id=str(uuid.uuid4()),
                session_type=session_type,
                model_used=model_used,
                outputs=result.insights or [],
                staging_ids=[staging_id] if staging_id else [],
                created_at=sched._clock().isoformat(),
            )
        except Exception:
            logger.error("Failed to write brainstorm_log entry", exc_info=True)


async def _signal_autonomy_success() -> None:
    """Signal autonomy calibration for background cognitive work."""
    try:
        from genesis.runtime import GenesisRuntime
        rt = GenesisRuntime.instance()
        mgr = getattr(rt, "_autonomy_manager", None)
        if mgr is not None:
            await mgr.record_success("background_cognitive")
    except Exception:
        logger.debug("Autonomy success signal failed (non-fatal)", exc_info=True)


def _filter_tiers_for_network(tiers: list[ComputeTier]) -> list[ComputeTier]:
    """Strip cloud compute tiers when the network is hard-OFFLINE (PR-3).

    Keeps surplus outage-aware: during a detected outage, tasks that need the
    internet stay ``pending`` (``next_task`` simply selects nothing for the
    stripped tiers, and re-selects them the instant connectivity returns)
    instead of dispatching into a dead network and churning ``failed`` /
    dead-letter rows every cycle. LAN-local compute (``LOCAL_30B``) still runs.

    This is dispatch-side *compute-availability* gating — the same mechanism as
    the existing LM-Studio reachability check in ``get_available_tiers`` — NOT
    generator alert-awareness (surplus generators stay deliberately blind to
    infrastructure alerts). Fail-safe: the parking lever off, a non-fresh/absent
    OFFLINE signal, or any error leaves the tier list untouched.
    """
    try:
        from genesis.resilience import network_config

        decision = network_config.parking_decision()
        if decision in ("off", "normal"):
            return tiers
        local_only = [t for t in tiers if t == ComputeTier.LOCAL_30B]
        if local_only == tiers:
            return tiers  # already LAN-only (or empty) — nothing cloud to strip
        if decision == "shadow":
            logger.info(
                "network OFFLINE: WOULD restrict surplus to LAN compute "
                "(shadow mode) — proceeding",
            )
            return tiers
        logger.info(
            "network OFFLINE: surplus restricted to LAN compute (%d→%d tiers)",
            len(tiers),
            len(local_only),
        )
        return local_only
    except Exception:
        logger.debug(
            "surplus network tier filter errored — leaving tiers intact",
            exc_info=True,
        )
        return tiers


async def dispatch_once(sched: DispatchContext) -> bool:
    """Single dispatch cycle. Returns True if a task was processed."""
    # Setup phases (recover/drain/reap/select) are TIMED but NOT heavy_workload-covered.
    # `_timed_phase` logs a WARNING if any phase runs abnormally long — the diagnostic
    # for the 2026-09-01 incident's still-unidentified "zombie restart with NO task
    # running" mechanism (localizes a stalling setup phase). We deliberately do NOT
    # forgive a setup-phase stall: (1) a genuine stall here is a real hang the 900s
    # watchdog SHOULD catch, and (2) the heavy_workload flag cannot be reliably published
    # while a setup phase holds the shared DB connection anyway — status_writer awaits
    # DB-backed counts before reading the flag (Codex #1588 P1). heavy_workload is scoped
    # to the EXECUTOR below, the one case it reliably covers (a long j9 executor that does
    # not hold the DB lock). Robust setup-phase coverage waits on the instrumentation
    # identifying the real mechanism (heavy_workload-hardening follow-up).
    with _timed_phase("recover_stuck"):
        await sched._queue.recover_stuck()

    with _timed_phase("drain_expired"):
        await sched._queue.drain_expired(max_age_hours=sched._task_expiry_hours)

    # Age-cap terminal rows (completed/failed/cancelled) so they don't accumulate
    # forever — drain_expired only touches pending.
    with _timed_phase("reap_terminal"):
        await sched._queue.reap_terminal(older_than_days=sched._terminal_retention_days)

    # Check idle
    if not sched._idle_detector.is_idle():
        return False

    with _timed_phase("get_available_tiers"):
        available_tiers = await sched._compute.get_available_tiers()

    # Outage awareness (PR-3): when the network is hard-OFFLINE, strip cloud tiers so
    # internet-dependent tasks stay `pending` instead of churning into a dead network.
    available_tiers = _filter_tiers_for_network(available_tiers)

    with _timed_phase("next_task"):
        task = await sched._queue.next_task(available_tiers)
    if task is None:
        return False

    # Execute
    logger.info("Dispatching surplus task %s (%s)", task.id, task.task_type)
    await sched._queue.mark_running(task.id)

    executor = _select_executor(sched, task)

    # Flag the EXECUTOR (+ intake routing + completion) as a heavy workload so the 900s
    # zombie-scheduler watchdog FORGIVES a single long-running task (e.g. j9_eval_batch
    # ~25 min) that would otherwise gap the dispatch-loop heartbeat and trigger a restart.
    # Covers ALL long surplus executors (j9, model_eval, wing_audit, code_audit). The
    # runtime flag auto-expires after 2h (runtime/_core.py), so a genuinely HUNG task is
    # still caught — forgives long, not dead. (Mirrors the dream jobs; direct-attr writes
    # match dream.py.) CLAIM ONLY A FREE SLOT (a single process-wide slot the dream jobs
    # also use) and clear only what WE set.
    #
    # KNOWN LIMITATIONS (pre-existing to the single-slot flag; tracked in the
    # heavy_workload-hardening follow-up, NOT fixed here): the slot is not ref-counted, so
    # a concurrent dream that stomps+clears it can drop this dispatch's protection mid-run
    # (rare: dream overlap + long dispatch + dream finishes first); and the flag reaches
    # the watchdog only via status.json, which the status writer builds AFTER DB-backed
    # counts, so a DB stall can hide it. Both need a ref-counted + DB-independent publish.
    from datetime import UTC, datetime

    from genesis.runtime import GenesisRuntime

    _hw_label = f"surplus:{task.task_type}"
    _hw_rt = None
    _hw_did_set = False
    with contextlib.suppress(Exception):
        _rt = GenesisRuntime.instance()
        if _rt._heavy_workload is None:  # free slot only — never stomp dream's flag
            _rt._heavy_workload = _hw_label
            _rt._heavy_workload_since = datetime.now(UTC)
            _hw_rt, _hw_did_set = _rt, True

    try:
        try:
            result = await executor.execute(task)
        except Exception:
            logger.exception("Surplus task %s failed with exception", task.id)
            await _handle_failure(sched, task, "executor_exception", emit_event=True)
            return False

        if not result.success:
            await _handle_failure(
                sched, task, result.error or "unknown", emit_event=False
            )
            return False

        # Route through intake pipeline (atomize → score → route to knowledge)
        staging_id, outcome_quality, judge_score, judge_detail = await _route_insights(
            sched, task, result,
        )

        await sched._queue.mark_completed(
            task.id, staging_id=staging_id, outcome_quality=outcome_quality,
            judge_score=judge_score, judge_detail=judge_detail,
        )

        await _chain_pipeline(sched, task, result)
        await _bridge_code_audit_findings(task, result)
        await _log_brainstorm(sched, task, result, staging_id)
        await _signal_autonomy_success()

        logger.info("Surplus task %s completed (staging=%s)", task.id, staging_id)
        return True
    finally:
        # Clear only what WE claimed, and only if still ours (label-guarded).
        if _hw_did_set and _hw_rt is not None:
            with contextlib.suppress(Exception):
                if _hw_rt._heavy_workload == _hw_label:
                    _hw_rt._heavy_workload = None
                    _hw_rt._heavy_workload_since = None


async def maybe_observe_failure(sched: DispatchContext, task, reason: str) -> None:
    """Create an observation if a task type has 3+ consecutive failures."""
    try:
        from genesis.db.crud import observations, surplus_tasks

        count = await surplus_tasks.consecutive_failures(
            sched._db, str(task.task_type),
        )
        if count >= 3:
            obs_id = f"surplus_failing_{task.task_type}"
            await observations.upsert(
                sched._db,
                id=obs_id,
                source="surplus_monitor",
                type="surplus_task_failing",
                content=(
                    f"Surplus task {task.task_type} has failed "
                    f"{count} consecutive times. Last reason: {reason}"
                ),
                priority="high",
                category="infrastructure",
                created_at=sched._clock().isoformat(),
            )
            logger.warning(
                "Surplus task %s: %d consecutive failures, observation created",
                task.task_type, count,
            )
    except Exception:
        logger.debug("Failed to create failure observation", exc_info=True)


# GROUNDWORK(v4-parallel-dispatch): dispatch multiple tasks concurrently
