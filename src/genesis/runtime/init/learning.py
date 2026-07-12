"""Init function: _init_learning."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.env import cc_project_dir, user_timezone

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def _wire_drip_retention_jobs(scheduler, rt) -> None:
    """Register restart-safe 90d retention jobs for the genesis.db drip tables.

    Extracted as a testable seam (cf. ``surplus._wire_memory_extraction_job``) so the
    registration itself is covered, not just the crud prune functions. execution_traces
    (per execution), cost_events (per LLM call), and the file_modifications audit trail
    (per file edit) each accrue a steady drip with no prior scheduled GC. Staggered
    off-peak after otel_span_prune; cost_events 90d stays well clear of the monthly budget
    window (``cost_tracker._period_start('this_month')`` looks back <= ~31d).
    """
    from apscheduler.triggers.cron import CronTrigger

    async def _prune_execution_traces() -> None:
        if rt._db is None:
            return
        try:
            from genesis.db.crud import execution_traces as _et

            removed = await _et.prune_older_than(rt._db, days=90)
            rt.record_job_success("execution_traces_prune")
            if removed:
                logger.info("execution_traces prune: removed %d rows (>90d)", removed)
        except Exception as exc:
            rt.record_job_failure("execution_traces_prune", str(exc))
            logger.exception("execution_traces prune failed")

    scheduler.add_job(
        _prune_execution_traces,
        CronTrigger(hour=4, minute=40, timezone=user_timezone()),
        id="execution_traces_prune",
        max_instances=1,
        misfire_grace_time=3600,
    )

    async def _prune_cost_events() -> None:
        if rt._db is None:
            return
        try:
            from genesis.db.crud import cost_events as _ce

            removed = await _ce.prune_older_than(rt._db, days=90)
            rt.record_job_success("cost_events_prune")
            if removed:
                logger.info("cost_events prune: removed %d rows (>90d)", removed)
        except Exception as exc:
            rt.record_job_failure("cost_events_prune", str(exc))
            logger.exception("cost_events prune failed")

    scheduler.add_job(
        _prune_cost_events,
        CronTrigger(hour=4, minute=50, timezone=user_timezone()),
        id="cost_events_prune",
        max_instances=1,
        misfire_grace_time=3600,
    )

    async def _prune_file_modifications() -> None:
        if rt._db is None:
            return
        try:
            from genesis.db.crud import file_modifications as _fm

            removed = await _fm.prune_older_than(rt._db, days=90)
            rt.record_job_success("file_modifications_prune")
            if removed:
                logger.info("file_modifications prune: removed %d rows (>90d)", removed)
        except Exception as exc:
            rt.record_job_failure("file_modifications_prune", str(exc))
            logger.exception("file_modifications prune failed")

    scheduler.add_job(
        _prune_file_modifications,
        CronTrigger(hour=5, minute=0, timezone=user_timezone()),
        id="file_modifications_prune",
        max_instances=1,
        misfire_grace_time=3600,
    )


def _wire_git_health_deep_job(scheduler, rt) -> None:
    """Register the daily `git fsck --full` deep check (F.1).

    The per-tick awareness probe (`awareness/loop._check_git_health`) is cheap +
    structural; this catches deeper loose-object/reachability corruption that
    only fsck finds — the class the thin-pool outage created. CronTrigger, NOT
    IntervalTrigger: an interval resets on restart and, on a box that restarts
    more often than the interval, would never fire. 04:10 sits just before the
    drip-prune jobs (04:40+). Extracted as a testable seam."""
    from apscheduler.triggers.cron import CronTrigger

    async def _git_health_deep() -> None:
        try:
            from genesis.observability import git_health

            report = await git_health.check_git_deep()
            git_health.write_git_health_verdict(report)
            rt.record_job_success("git_health_deep")
            if not report.ok:
                failures = ", ".join(report.failures)
                logger.error("git fsck deep check FAILED: %s", failures)
                if rt._db is not None:
                    import uuid
                    from datetime import UTC, datetime

                    from genesis.db.crud import observations

                    await observations.create(
                        rt._db,
                        id=str(uuid.uuid4()),
                        source="git_health_monitor",
                        type="infrastructure_alert",
                        content=(
                            f"`git fsck --full` reported problems ({failures}) — objects are "
                            "missing or corrupt (incl. zeroed-but-present blobs), which disables "
                            "the guardian's REVERT_CODE lever. Diagnose and repair the local "
                            "git in ~/genesis — see docs/reference/recovery-and-portability-workflow.md."
                        ),
                        priority="critical",
                        created_at=datetime.now(UTC).isoformat(),
                    )
        except Exception as exc:
            rt.record_job_failure("git_health_deep", str(exc))
            logger.exception("git health deep check failed")

    scheduler.add_job(
        _git_health_deep,
        CronTrigger(hour=4, minute=10, timezone=user_timezone()),
        id="git_health_deep",
        max_instances=1,
        misfire_grace_time=3600,
    )


async def init(rt: GenesisRuntime) -> None:
    """Initialize learning pipeline, triage, calibration, harvest, and all scheduled jobs."""
    if rt._db is None or rt._router is None:
        logger.warning(
            "Learning skipped — missing prerequisites (db=%s, router=%s)",
            rt._db is not None,
            rt._router is not None,
        )
        return

    try:
        if rt._awareness_loop is not None:
            from genesis.awareness.signals import (
                ContainerMemoryCollector,
                ProcessHealthCollector,
                StrategicTimerCollector,
            )
            from genesis.env import ollama_enabled
            from genesis.learning.signals.autonomy_activity import (
                AutonomyActivityCollector,
            )
            from genesis.learning.signals.budget import BudgetCollector
            from genesis.learning.signals.conversation import ConversationCollector
            from genesis.learning.signals.critical_failure import (
                CriticalFailureCollector,
            )
            from genesis.learning.signals.error_spike import ErrorSpikeCollector
            from genesis.learning.signals.guardian_activity import (
                GuardianActivityCollector,
            )
            from genesis.learning.signals.light_cascade import LightCascadeCollector
            from genesis.learning.signals.micro_cascade import MicroCascadeCollector
            from genesis.learning.signals.outreach_engagement import (
                OutreachEngagementCollector,
            )
            from genesis.learning.signals.pending_items import (
                PendingItemCollector,
            )
            from genesis.learning.signals.recon_findings import (
                ReconFindingsCollector,
            )
            from genesis.learning.signals.sentinel_activity import (
                SentinelActivityCollector,
            )
            from genesis.learning.signals.surplus_activity import (
                SurplusActivityCollector,
            )
            from genesis.learning.signals.task_quality import TaskQualityCollector
            from genesis.learning.signals.user_goal_staleness import (
                UserGoalStalenessCollector,
            )
            from genesis.learning.signals.user_session_pattern import (
                UserSessionPatternCollector,
            )
            from genesis.observability.health import (
                probe_db,
                probe_ollama,
                probe_qdrant,
            )

            # DB and Qdrant are non-optional — Genesis cannot function
            # without them, so their absence is a critical_failure. Ollama
            # is opt-in (cloud-primary architecture): only treat its
            # absence as critical when the install configures it as
            # enabled. Without this gate, every cloud-only install would
            # fire critical_failure=1.0 forever on a service that was
            # never required, polluting reflections and observation
            # writes with phantom emergencies.
            probes = [
                partial(probe_db, rt._db),
                probe_qdrant,
            ]
            if ollama_enabled():
                probes.append(probe_ollama)

            from genesis.learning.signals.cc_version import CCVersionCollector
            from genesis.learning.signals.genesis_version import GenesisVersionCollector

            collectors = [
                ConversationCollector(rt._db),
                TaskQualityCollector(rt._db),
                OutreachEngagementCollector(rt._db),
                ReconFindingsCollector(rt._db),
                BudgetCollector(rt._db),
                ErrorSpikeCollector(rt._db),
                CriticalFailureCollector(probes),
                StrategicTimerCollector(rt._db),
                ContainerMemoryCollector(),
                PendingItemCollector(rt._db),
                MicroCascadeCollector(rt._db),
                LightCascadeCollector(rt._db),
                SentinelActivityCollector(),
                GuardianActivityCollector(),
                SurplusActivityCollector(rt._db),
                AutonomyActivityCollector(rt._db),
                GenesisVersionCollector(
                    rt._db,
                    pipeline_getter=lambda: rt._outreach_pipeline,
                ),
                CCVersionCollector(
                    rt._db,
                    router=rt._router,
                    pipeline_getter=lambda: rt._outreach_pipeline,
                    memory_store_getter=lambda: rt._memory_store,
                ),
                ProcessHealthCollector(),
                UserGoalStalenessCollector(rt._db),
                UserSessionPatternCollector(rt._db),
            ]
            rt._awareness_loop.replace_collectors(collectors)
            logger.info("Installed %d signal collectors", len(collectors))

        from genesis.learning.classification.delta import DeltaAssessor
        from genesis.learning.classification.outcome import OutcomeClassifier
        from genesis.learning.observation_writer import ObservationWriter
        from genesis.learning.pipeline import build_triage_pipeline
        from genesis.learning.triage.calibration import TriageCalibrator
        from genesis.learning.triage.classifier import TriageClassifier

        triage_classifier = TriageClassifier(rt._router)
        outcome_classifier = OutcomeClassifier(rt._router)
        delta_assessor = DeltaAssessor(rt._router)
        rt._observation_writer = ObservationWriter(memory_store=rt._memory_store)
        observation_writer = rt._observation_writer

        rt._triage_pipeline = build_triage_pipeline(
            db=rt._db,
            triage_classifier=triage_classifier,
            outcome_classifier=outcome_classifier,
            delta_assessor=delta_assessor,
            observation_writer=observation_writer,
            event_bus=rt._event_bus,
            router=rt._router,
            runtime=rt,
            identity_loader=getattr(rt, "_identity_loader", None),
        )
        logger.info("Genesis triage pipeline created")

        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        calibrator = TriageCalibrator(
            rt._router,
            rt._db,
            memory_store=rt._memory_store,
            event_bus=rt._event_bus,
        )

        rt._learning_scheduler = AsyncIOScheduler()

        _original_calibration = calibrator.run_daily_calibration

        async def _calibration_with_health() -> None:
            try:
                if rt.paused:
                    logger.debug("Triage calibration skipped (Genesis paused)")
                    return
            except Exception:
                logger.warning(
                    "Pause check failed — skipping calibration as precaution", exc_info=True
                )
                return
            try:
                result = await _original_calibration()
                if result is None:
                    rt.record_job_success("triage_calibration_daily")
                    logger.info("Triage calibration: no data or validation failed, skipped")
                else:
                    rt.record_job_success("triage_calibration_daily")
            except Exception as exc:
                rt.record_job_failure("triage_calibration_daily", str(exc))
                raise

        rt._learning_scheduler.add_job(
            _calibration_with_health,
            CronTrigger(hour=3, minute=0, timezone=user_timezone()),
            id="triage_calibration_daily",
            max_instances=1,
            misfire_grace_time=3600,
        )

        # WS-8: email autonomy gate resolution watcher — the correctness
        # guarantee for held email sends. Drains pending_email_sends: approved →
        # send below the gate + record_success; rejected → record_correction;
        # orphaned/expired → close out. max_instances=1 ⇒ no in-drain races.
        from genesis.autonomy.email_gate_watcher import drain_pending_email_sends

        async def _email_gate_drain() -> None:
            try:
                if rt.paused:
                    return
            except Exception:
                logger.warning(
                    "Pause check failed — skipping email gate drain",
                    exc_info=True,
                )
                return
            try:
                n = await drain_pending_email_sends(rt)
                rt.record_job_success("email_gate_drain")
                if n:
                    logger.info("Email gate drain resolved %d held send(s)", n)
            except Exception as exc:
                rt.record_job_failure("email_gate_drain", str(exc))
                raise

        rt._learning_scheduler.add_job(
            _email_gate_drain,
            CronTrigger(minute="*/5", timezone=user_timezone()),
            id="email_gate_drain",
            max_instances=1,
            misfire_grace_time=300,
        )

        # WS-8 PR-D capability staleness decay — a GRANTED email cell idle past
        # the half-life lapses back to NOT_DETERMINED (holds again on next use),
        # so standing autonomy can't entrench unused. Daily, INFO-level.
        from genesis.db.crud import capability_grants as _cg

        async def _capability_decay_sweep() -> None:
            try:
                if rt.paused:
                    return
            except Exception:
                logger.warning(
                    "Pause check failed — skipping capability decay",
                    exc_info=True,
                )
                return
            try:
                decayed = await _cg.decay_stale_cells(
                    rt._db,
                    now=datetime.now(UTC).isoformat(),
                )
                rt.record_job_success("capability_decay_sweep")
                if decayed:
                    logger.info(
                        "Capability decay: %d stale grant(s) lapsed: %s",
                        len(decayed),
                        ", ".join(decayed),
                    )
            except Exception as exc:
                rt.record_job_failure("capability_decay_sweep", str(exc))
                raise

        rt._learning_scheduler.add_job(
            _capability_decay_sweep,
            CronTrigger(hour=3, minute=30, timezone=user_timezone()),
            id="capability_decay_sweep",
            max_instances=1,
            misfire_grace_time=3600,
        )

        from genesis.db.crud import observations
        from genesis.learning.harvesting.auto_memory import harvest_auto_memory

        def _classify_memory_type(filename: str) -> str:
            name = filename.lower()
            if name == "memory.md":
                return "memory_index"
            if name.startswith("build"):
                return "build_state"
            if name.startswith("feedback_"):
                return "feedback_rule"
            if name.startswith("project_"):
                return "project_context"
            if name.startswith("user_"):
                return "user_profile"
            if name.startswith("reference_"):
                return "reference_pointer"
            return "cc_memory_file"

        async def _harvest_and_store() -> None:
            try:
                memory_dir = Path.home() / ".claude" / "projects" / cc_project_dir() / "memory"
                items = harvest_auto_memory(memory_dir)
                stored = 0
                skipped = 0
                for item in items:
                    content = item.get("content", "")[:2000]
                    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

                    if await observations.exists_by_hash(
                        rt._db,
                        source="auto_memory_harvest",
                        content_hash=content_hash,
                    ):
                        skipped += 1
                        continue

                    obs_type = _classify_memory_type(item.get("file", ""))
                    await observation_writer.write(
                        rt._db,
                        source="auto_memory_harvest",
                        type=obs_type,
                        content=content,
                        priority="low",
                        content_hash=content_hash,
                    )
                    stored += 1
                if items:
                    logger.info(
                        "Harvested %d auto-memory items (%d new, %d dedup-skipped)",
                        len(items),
                        stored,
                        skipped,
                    )
                rt.record_job_success("auto_memory_harvest")
            except Exception as exc:
                rt.record_job_failure("auto_memory_harvest", str(exc))
                logger.exception("Auto-memory harvest failed")

        rt._learning_scheduler.add_job(
            _harvest_and_store,
            CronTrigger(hour="*/6", minute=15, timezone=user_timezone()),
            id="auto_memory_harvest",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _observation_expiry_sweep() -> None:
            try:
                count = await observations.resolve_expired(rt._db)
                stale = await observations.resolve_stale_persistent(rt._db, max_age_days=60)
                rt.record_job_success("observation_expiry_sweep")
                if count or stale:
                    logger.info(
                        "Observation expiry sweep: resolved %d expired, %d stale persistent",
                        count,
                        stale,
                    )
            except Exception as exc:
                rt.record_job_failure("observation_expiry_sweep", str(exc))
                logger.exception("Observation expiry sweep failed")

        rt._learning_scheduler.add_job(
            _observation_expiry_sweep,
            CronTrigger(hour=2, minute=0, timezone=user_timezone()),
            id="observation_expiry_sweep",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _follow_up_retention_sweep() -> None:
            try:
                from genesis.db.crud import follow_ups

                count = await follow_ups.purge_completed(rt._db)
                rt.record_job_success("follow_up_retention_sweep")
                if count:
                    logger.info(
                        "Follow-up retention sweep: purged %d old records",
                        count,
                    )
            except Exception as exc:
                rt.record_job_failure("follow_up_retention_sweep", str(exc))
                logger.exception("Follow-up retention sweep failed")

        rt._learning_scheduler.add_job(
            _follow_up_retention_sweep,
            CronTrigger(hour=2, minute=30, timezone=user_timezone()),
            id="follow_up_retention_sweep",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _inbox_marker_decay_sweep() -> None:
            # Soft-age-out stale inbox WATCH/BOOKMARK attention markers (the
            # tabled lane): status→completed after 60d. Mechanical TTL, so it
            # lives here (learning scheduler), NOT in the ego — the ego has no
            # authority to close a user-curated marker.
            try:
                if rt.paused:
                    return
            except Exception:
                logger.warning(
                    "Pause check failed — skipping inbox marker decay",
                    exc_info=True,
                )
                return
            try:
                from genesis.db.crud import follow_ups

                decayed = await follow_ups.decay_stale_inbox_markers(rt._db)
                rt.record_job_success("inbox_marker_decay")
                if decayed:
                    logger.info(
                        "Inbox marker decay: aged out %d stale attention marker(s)",
                        decayed,
                    )
            except Exception as exc:
                rt.record_job_failure("inbox_marker_decay", str(exc))
                logger.exception("Inbox marker decay sweep failed")

        rt._learning_scheduler.add_job(
            _inbox_marker_decay_sweep,
            CronTrigger(hour=4, minute=35, timezone=user_timezone()),
            id="inbox_marker_decay",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _run_recovery() -> None:
            if rt._recovery_orchestrator is not None:
                try:
                    report = await rt._recovery_orchestrator.run_recovery()
                    rt.record_job_success("recovery_orchestrator")
                    if report.dead_letters_replayed or report.embeddings_recovered:
                        logger.info(
                            "Recovery: replayed=%d embeddings=%d pending=%d",
                            report.dead_letters_replayed,
                            report.embeddings_recovered,
                            report.items_pending,
                        )
                except Exception:
                    rt.record_job_failure("recovery_orchestrator", "recovery failed")
                    logger.exception("Recovery orchestrator failed")

        rt._learning_scheduler.add_job(
            _run_recovery,
            IntervalTrigger(minutes=30),
            id="recovery_orchestrator",
            max_instances=1,
            misfire_grace_time=300,
        )

        async def _validate_keys() -> None:
            if rt._health_data is not None:
                try:
                    await rt._health_data.validate_api_keys()
                    rt.record_job_success("api_key_validation")
                except Exception:
                    rt.record_job_failure("api_key_validation", "validation failed")
                    logger.exception("API key validation failed")

        rt._learning_scheduler.add_job(
            _validate_keys,
            IntervalTrigger(minutes=30),
            id="api_key_validation",
            max_instances=1,
            misfire_grace_time=300,
        )
        from genesis.util.tasks import tracked_task as _tt

        _tt(_validate_keys(), name="initial_api_key_validation")
        # Boot kick: run recovery housekeeping (dead-letter replay, stuck-
        # processing expiry, pending embeddings) immediately instead of
        # waiting up to 30 min for the first interval fire. The expiry's own
        # 2h age gate still applies to each row.
        _tt(_run_recovery(), name="initial_recovery")

        async def _expire_dead_letters() -> None:
            if rt._dead_letter_queue is not None:
                try:
                    expired = await rt._dead_letter_queue.expire_old(max_age_hours=72)
                    rt.record_job_success("dead_letter_expiry")
                    if expired:
                        logger.info("Expired %d dead letter items (>72h)", expired)
                except Exception:
                    rt.record_job_failure("dead_letter_expiry", "expiry failed")
                    logger.exception("Dead letter expiry failed")

        rt._learning_scheduler.add_job(
            _expire_dead_letters,
            CronTrigger(hour=1, minute=30, timezone=user_timezone()),
            id="dead_letter_expiry",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _expire_stale_messages() -> None:
            try:
                from genesis.db.crud import message_queue

                now = datetime.now(UTC).isoformat()
                expired = await message_queue.expire_older_than(
                    rt._db,
                    max_age_hours=168,
                    expired_at=now,
                )
                rt.record_job_success("message_queue_expiry")
                if expired:
                    logger.info("Expired %d stale message queue items (>7d)", expired)
            except Exception as exc:
                rt.record_job_failure("message_queue_expiry", str(exc))
                logger.exception("Message queue expiry failed")

        rt._learning_scheduler.add_job(
            _expire_stale_messages,
            CronTrigger(hour=2, minute=30, timezone=user_timezone()),
            id="message_queue_expiry",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _redispatch_dead_letters() -> None:
            try:
                if rt.paused:
                    logger.debug("Dead letter redispatch skipped (Genesis paused)")
                    return
            except Exception:
                logger.warning(
                    "Pause check failed — skipping redispatch as precaution", exc_info=True
                )
                return
            if rt._dead_letter_queue is not None and rt._router is not None:
                try:
                    ok, fail = await rt._dead_letter_queue.redispatch(
                        rt._router.route_call,
                    )
                    rt.record_job_success("dead_letter_redispatch")
                    if ok or fail:
                        logger.info(
                            "Dead letter redispatch: %d succeeded, %d failed",
                            ok,
                            fail,
                        )
                except Exception:
                    rt.record_job_failure(
                        "dead_letter_redispatch",
                        "redispatch failed",
                    )
                    logger.exception("Dead letter redispatch failed")

        rt._learning_scheduler.add_job(
            _redispatch_dead_letters,
            CronTrigger(hour="0,6,12,18", minute=45, timezone=user_timezone()),
            id="dead_letter_redispatch",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _run_procedure_promotion() -> None:
            try:
                from genesis.learning.procedural.promoter import promote_and_demote

                result = await promote_and_demote(rt._db)
                rt.record_job_success("procedure_promotion")
                if any(v > 0 for v in result.values()):
                    logger.info("Procedure promotion: %s", result)
            except Exception as exc:
                rt.record_job_failure("procedure_promotion", str(exc))
                logger.exception("Procedure promotion failed")
            # Self-healing embedding backfill — independent of promotion so a
            # backfill failure never fails the promotion job. Repairs procedures
            # whose principle_embedding is NULL (transient embed failure at
            # create) so they rejoin the proactive-surfacing pool.
            try:
                from genesis.learning.procedural.promoter import (
                    backfill_missing_embeddings,
                )

                await backfill_missing_embeddings(rt._db)
            except Exception:
                logger.exception("Procedure embedding backfill failed")

        # CronTrigger instead of IntervalTrigger: IntervalTrigger resets its
        # countdown on every server restart, so under frequent restarts the job
        # can keep slipping its hour and never fire (same class of bug that bit
        # user_model_evolution / process_reaper).  Runs at :30 past every hour
        # (:15 is the process_reaper; hour-boundary minutes carry heavier jobs).
        rt._learning_scheduler.add_job(
            _run_procedure_promotion,
            CronTrigger(minute=30),
            id="procedure_promotion",
            max_instances=1,
            misfire_grace_time=600,
        )

        from genesis.memory.user_model import UserModelEvolver

        user_model_evolver = UserModelEvolver(db=rt._db)

        async def _evolve_user_model() -> None:
            try:
                result = await user_model_evolver.process_pending_deltas()
                if result:
                    logger.info(
                        "User model evolved to v%d (%d evidence)",
                        result.version,
                        result.evidence_count,
                    )
                    # USER.md auto-synthesis is PERMANENTLY DISABLED — USER.md
                    # is user-edited only. Instead, synthesize USER_KNOWLEDGE.md
                    # (system-owned cache, safe to overwrite).
                    #
                    # Synthesis path: try call site 11 (LLM narrative via the
                    # router's free chain) first. If that fails (all free
                    # providers exhausted, malformed response), fall back to
                    # the rules-based dict rendering. Either way the file
                    # gets refreshed.
                    identity_loader = getattr(rt, "_identity_loader", None)
                    if identity_loader is None:
                        logger.warning(
                            "identity_loader not available, skipping USER_KNOWLEDGE.md synthesis",
                        )
                    else:
                        narrative: str | None = None
                        if rt._router is not None:
                            try:
                                narrative = await user_model_evolver.synthesize_narrative(
                                    rt._router,
                                    evidence_count=result.evidence_count,
                                )
                            except Exception:
                                logger.exception(
                                    "synthesize_narrative raised — falling "
                                    "back to rules-based rendering",
                                )
                        try:
                            # Capture the pre-image before the loader overwrites it,
                            # then record into the cognitive self-mod ledger so a bad
                            # synthesis can be rolled back (Option B: no loader change).
                            uk_path = identity_loader._dir / "USER_KNOWLEDGE.md"
                            prior_uk = (
                                uk_path.read_text(encoding="utf-8") if uk_path.exists() else None
                            )
                            identity_loader.write_user_knowledge_md(
                                result.model,
                                evidence_count=result.evidence_count,
                                narrative=narrative,
                            )
                            try:
                                from genesis.learning.cognitive_ledger import (
                                    record_existing,
                                )

                                await record_existing(
                                    rt._db,
                                    actor="user_model_evolution",
                                    path=uk_path,
                                    prior_content=prior_uk,
                                    applied_content=uk_path.read_text(encoding="utf-8"),
                                    summary="USER_KNOWLEDGE synthesis ("
                                    + ("narrative" if narrative else "rules")
                                    + ")",
                                    metadata={
                                        "evidence_count": result.evidence_count,
                                        "mode": "narrative" if narrative else "rules",
                                    },
                                )
                            except Exception:
                                logger.warning(
                                    "cognitive_ledger: USER_KNOWLEDGE record failed "
                                    "(write unaffected)",
                                    exc_info=True,
                                )
                            if narrative:
                                logger.info(
                                    "USER_KNOWLEDGE.md updated via LLM synthesis (call site 11)",
                                )
                            else:
                                logger.info(
                                    "USER_KNOWLEDGE.md updated via rules "
                                    "fallback (no narrative available)",
                                )
                        except Exception:
                            logger.exception("Failed to synthesize USER_KNOWLEDGE.md")
                rt.record_job_success("user_model_evolution")
            except Exception as exc:
                rt.record_job_failure("user_model_evolution", str(exc))
                logger.exception("User model evolution failed")

        rt._learning_scheduler.add_job(
            _evolve_user_model,
            CronTrigger(
                hour=6, minute=30, timezone=user_timezone()
            ),  # moved from 4:30 to avoid dream cycle window
            id="user_model_evolution",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _reap_stale_sessions() -> None:
            if rt._db is None:
                return
            try:
                from genesis.db.crud.session_heartbeats import (
                    cleanup_stale as cleanup_stale_heartbeats,
                )

                # Policy-aware sweep via SessionManager — stale
                # non-foreground 'active' rows → 'expired' (outcome UNKNOWN:
                # the process is gone; it may have crashed or been killed),
                # with session end-hooks fired. Replaces the old crud
                # reap_stale, which relabeled these rows 'completed' and made
                # crashes read as successes in J-9's success rates.
                reaped = 0
                if rt._session_manager is not None:
                    reaped = await rt._session_manager.cleanup_stale(
                        max_idle_minutes=360,
                    )
                cleaned = await cleanup_stale_heartbeats(rt._db)
                rt.record_job_success("session_reaper")
                if reaped:
                    logger.info("Session reaper: expired %d stale sessions", reaped)
                if cleaned:
                    logger.info("Session reaper: cleaned %d stale heartbeats", cleaned)
            except Exception as exc:
                rt.record_job_failure("session_reaper", str(exc))
                logger.exception("Session reaper failed")

        rt._learning_scheduler.add_job(
            _reap_stale_sessions,
            CronTrigger(hour="1,7,13,19", minute=30, timezone=user_timezone()),
            id="session_reaper",
            max_instances=1,
            misfire_grace_time=3600,
        )
        # The boot-time sweep kick for this job lives at the END of
        # GenesisRuntime.bootstrap() (not here) — session end-hooks (e.g. the
        # ego's dispatch-outcome tracker) register during LATER init steps,
        # and a sweep fired from learning init would expire orphaned rows
        # before those hooks exist.

        async def _refresh_capability_map() -> None:
            if rt._db is None:
                return
            try:
                from genesis.ego.capability_aggregator import refresh_capability_map

                count = await refresh_capability_map(rt._db)
                rt.record_job_success("capability_map_refresh")
                if count:
                    logger.info("Capability map refreshed: %d domains", count)
            except Exception as exc:
                rt.record_job_failure("capability_map_refresh", str(exc))
                logger.exception("Capability map refresh failed")

        rt._learning_scheduler.add_job(
            _refresh_capability_map,
            CronTrigger(
                hour="9,21", minute=15, timezone=user_timezone()
            ),  # moved from 4:15/16:15 to avoid dream cycle window
            id="capability_map_refresh",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _run_outcome_harvest() -> None:
            # Fold existing siloed outcome signals into the outcome_events
            # ledger. DARK: nothing consumes the ledger yet, so this is
            # behaviour-neutral — it only populates a new table. The one-shot
            # backfill (guarded by an ego_state marker) runs once; run() keeps
            # the recent window fresh thereafter.
            if rt._db is None:
                return
            try:
                from genesis.feedback.harvest import OutcomeHarvester

                harvester = OutcomeHarvester(rt._db)
                backfill = await harvester.run_backfill()
                incremental = await harvester.run()
                rt.record_job_success("outcome_harvest")
                if not backfill.get("skipped"):
                    logger.info("Outcome backfill: %s", backfill)
                if any(incremental.values()):
                    logger.info("Outcome harvest: %s", incremental)
            except Exception as exc:
                rt.record_job_failure("outcome_harvest", str(exc))
                logger.exception("Outcome harvest failed")

        rt._learning_scheduler.add_job(
            _run_outcome_harvest,
            # 30 min before capability_map_refresh (9:15/21:15) in CLOCK time —
            # APScheduler fires by trigger time, not registration order — so the
            # map step (a future PR) reads a fresh harvest. Avoids the loaded
            # 0-6h windows (calibration, reapers, dream cycle, user-model).
            CronTrigger(hour="8,20", minute=45, timezone=user_timezone()),
            id="outcome_harvest",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _run_ego_calibration() -> None:
            # Compute the ego's confidence calibration from the Outcome Bus T1
            # rows and snapshot it (ECE-over-time trend). MEASURE-ONLY / DARK:
            # writes only ego_calibration_snapshots (no cognitive-path reader),
            # never injects back into the ego. Runs 15 min after outcome_harvest
            # (8:45/20:45) so it reads a fresh harvest, 15 min before
            # capability_map_refresh (9:15/21:15) — a clean WAL gap.
            if rt._db is None:
                return
            try:
                from genesis.feedback.calibration import compute_ego_calibration

                snap = await compute_ego_calibration(rt._db)
                rt.record_job_success("ego_calibration")
                if snap is not None:
                    logger.info(
                        "Ego calibration: ECE=%.4f n=%d%s",
                        snap["ece"],
                        snap["sample_count"],
                        " (low-confidence)" if snap["low_confidence"] else "",
                    )
            except Exception as exc:
                rt.record_job_failure("ego_calibration", str(exc))
                logger.exception("Ego calibration failed")

        rt._learning_scheduler.add_job(
            _run_ego_calibration,
            CronTrigger(hour="9,21", minute=0, timezone=user_timezone()),
            id="ego_calibration",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _reap_activity_log() -> None:
            if rt._activity_tracker is None:
                return
            try:
                reaped = await rt._activity_tracker.reap_old_records()
                rt.record_job_success("activity_log_reaper")
                if reaped:
                    logger.info("Activity log reaper: deleted %d old records", reaped)
            except Exception as exc:
                rt.record_job_failure("activity_log_reaper", str(exc))
                logger.exception("Activity log reaper failed")

        rt._learning_scheduler.add_job(
            _reap_activity_log,
            CronTrigger(hour="2,8,14,20", minute=0, timezone=user_timezone()),
            id="activity_log_reaper",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _ingest_cc_spans() -> None:
            """Drain CC-tool span flat-files into otel_spans (tracing backbone)."""
            if rt._db is None:
                return
            try:
                from genesis.observability.span_ingest import ingest_pending_spans

                n = await ingest_pending_spans(rt._db)
                rt.record_job_success("cc_span_ingest")
                if n:
                    logger.debug("CC span ingest: %d spans", n)
            except Exception as exc:
                rt.record_job_failure("cc_span_ingest", str(exc))
                logger.exception("CC span ingest failed")

        # Frequent (every 2 min) so dispatched-session tool spans land in the
        # trace shortly after the session runs. minute-cron survives restart
        # (no IntervalTrigger reset trap). No-op cost when idle (empty dir).
        rt._learning_scheduler.add_job(
            _ingest_cc_spans,
            CronTrigger(minute="*/2"),
            id="cc_span_ingest",
            max_instances=1,
            misfire_grace_time=60,
        )

        async def _prune_otel_spans() -> None:
            """Retention: delete spans older than config retention_days."""
            if rt._span_writer is None:
                return
            try:
                from genesis.observability.span_config import load_spans_config

                _, retention_days = load_spans_config()
                removed = await rt._span_writer.prune(older_than_days=retention_days)
                rt.record_job_success("otel_span_prune")
                if removed:
                    logger.info("otel_spans prune: removed %d old spans", removed)
            except Exception as exc:
                rt.record_job_failure("otel_span_prune", str(exc))
                logger.exception("otel_spans prune failed")

        rt._learning_scheduler.add_job(
            _prune_otel_spans,
            CronTrigger(hour=4, minute=30, timezone=user_timezone()),
            id="otel_span_prune",
            max_instances=1,
            misfire_grace_time=3600,
        )

        # genesis.db drip-table retention (restart-safe CronTrigger; extracted to a
        # testable seam so the registration is covered, not just the crud prunes).
        _wire_drip_retention_jobs(rt._learning_scheduler, rt)

        # Daily git fsck deep check (F.1) — catches loose-object/reachability
        # corruption the cheap per-tick probe can't. Restart-safe CronTrigger.
        _wire_git_health_deep_job(rt._learning_scheduler, rt)

        async def _reap_stale_processes() -> None:
            """Kill leaked processes older than their configured threshold.

            Targets:
              - opencode-ai: 24 hours (pgrep -f, matches command line)
              - claude: 7 days (pgrep -x, matches exact process name)

            Kills the full descendant tree (children, grandchildren) of each
            stale process to prevent orphaned MCP servers, Playwright, etc.
            """
            import asyncio
            import os

            from genesis.browser.types import BROWSER_PGREP_PATTERNS

            # (pgrep_flag, pattern, max_age_hours, label)
            targets = [
                ("-f", "opencode-ai", 24, "opencode-ai"),
                ("-x", "claude", 168, "claude"),  # 7 days
            ]
            # Browser processes — 4h max age. Idle timeout fires at 1h,
            # MCP lifespan fires on session end. A 4h-old browser process
            # has survived both layers and is definitively orphaned.
            for bp in BROWSER_PGREP_PATTERNS:
                targets.append(("-f", bp, 4, f"browser:{bp}"))
            my_pid = os.getpid()
            my_ppid = os.getppid()
            protected = {my_pid, my_ppid}

            async def _get_descendants(pid: int, depth: int = 0) -> list[int]:
                """Return all descendant PIDs (children-first / bottom-up)."""
                if depth >= 10:
                    return []
                proc = await asyncio.create_subprocess_exec(
                    "pgrep",
                    "-P",
                    str(pid),
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if not stdout.strip():
                    return []
                children = [
                    int(p.strip()) for p in stdout.decode().strip().split("\n") if p.strip()
                ]
                result: list[int] = []
                for child in children:
                    result.extend(await _get_descendants(child, depth + 1))
                result.extend(children)
                return result

            try:
                all_killed: list[tuple[int, str]] = []
                for flag, pattern, max_age_h, label in targets:
                    proc = await asyncio.create_subprocess_exec(
                        "pgrep",
                        flag,
                        pattern,
                        stdout=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    if not stdout.strip():
                        continue

                    max_age = max_age_h * 3600
                    clock_ticks = os.sysconf("SC_CLK_TCK")

                    for pid_str in stdout.decode().strip().split("\n"):
                        pid = int(pid_str.strip())
                        if pid <= 1 or pid in protected:
                            continue
                        try:
                            if not Path(f"/proc/{pid}/stat").exists():
                                continue
                            with open(f"/proc/{pid}/stat") as f:
                                raw = f.read()
                            # comm field (field 2) is in parens and may
                            # contain spaces; split after the last ')'.
                            after_comm = raw[raw.rfind(")") + 2 :]
                            start_ticks = int(after_comm.split()[19])
                            with open("/proc/uptime") as f:
                                uptime_secs = float(f.read().split()[0])
                            age_secs = uptime_secs - (start_ticks / clock_ticks)
                            if age_secs > max_age:
                                # Collect descendants bottom-up, then the root
                                tree = await _get_descendants(pid)
                                tree.append(pid)
                                for p in tree:
                                    if p <= 1 or p in protected:
                                        continue
                                    with contextlib.suppress(ProcessLookupError):
                                        os.kill(p, 15)  # SIGTERM
                                    all_killed.append((p, label))
                        except (ProcessLookupError, FileNotFoundError, ValueError):
                            continue

                if all_killed:
                    # 5s grace period for graceful shutdown — browsers need
                    # time to flush profile SQLite and release locks.
                    await asyncio.sleep(5)
                    for pid, _ in all_killed:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, 9)  # SIGKILL
                    logger.info(
                        "Process reaper: killed %d stale process(es): %s",
                        len(all_killed),
                        all_killed,
                    )
                    # Create observation for visibility
                    if rt._db is not None:
                        try:
                            import json
                            from datetime import UTC, datetime
                            from uuid import uuid4

                            from genesis.db.crud import observations

                            await observations.create(
                                rt._db,
                                id=f"reaper-{uuid4().hex[:8]}",
                                source="process_reaper",
                                type="process_reaper_kill",
                                priority="low",
                                content=json.dumps(
                                    {
                                        "killed_count": len(all_killed),
                                        "processes": [
                                            {"pid": p, "label": lbl} for p, lbl in all_killed
                                        ],
                                    }
                                ),
                                created_at=datetime.now(UTC).isoformat(),
                            )
                        except Exception:
                            logger.debug(
                                "Failed to create reaper observation",
                                exc_info=True,
                            )
                rt.record_job_success("process_reaper")
            except Exception as exc:
                rt.record_job_failure("process_reaper", str(exc))
                logger.exception("Process reaper failed")

        # CronTrigger instead of IntervalTrigger: IntervalTrigger resets on
        # server restart, so the reaper never fires if the server restarts
        # within the hour.  Runs at :15 past every hour to avoid collision
        # with hour-boundary jobs.
        rt._learning_scheduler.add_job(
            _reap_stale_processes,
            CronTrigger(minute=15),
            id="process_reaper",
            max_instances=1,
            misfire_grace_time=600,
        )

        # ── Skill evolution pipeline (weekly backup trigger) ────────────────
        async def _run_skill_evolution() -> None:
            try:
                from genesis.learning.skills.pipeline import SkillEvolutionPipeline

                outreach_fn = None
                outreach_pipeline = getattr(rt, "_outreach_pipeline", None)
                if outreach_pipeline is not None:
                    outreach_fn = outreach_pipeline.submit

                pipeline = SkillEvolutionPipeline(
                    db=rt._db,
                    router=rt._router,
                    outreach_fn=outreach_fn,
                )
                result = await pipeline.run()
                if result["proposed"] > 0 or result["applied"] > 0:
                    logger.info("Skill evolution completed: %s", result)
                rt.record_job_success("skill_evolution")
            except Exception as exc:
                rt.record_job_failure("skill_evolution", str(exc))
                logger.exception("Skill evolution pipeline failed")

        rt._learning_scheduler.add_job(
            _run_skill_evolution,
            CronTrigger(
                day_of_week="sat", hour=4, minute=0, timezone=user_timezone()
            ),  # moved off Sunday to avoid dream cycle
            id="skill_evolution",
            max_instances=1,
            misfire_grace_time=3600,
        )

        # J-9 eval weekly aggregation (Sundays 7am — after dream cycle clears).
        # Hard dep on 7-day rolling window — must stay Sunday.
        async def _run_j9_eval_aggregation():
            try:
                from genesis.eval.j9_aggregator import run_weekly_aggregation

                results = await run_weekly_aggregation(rt._db)
                dims_computed = len(results)
                logger.info("J9 weekly aggregation: %d dimensions computed", dims_computed)
                # Close the J-9 loop: a subsystem-grade regression becomes a
                # control path — a BLOCKER alert + a human-gated proposal. Wrapped
                # so a surfacing failure never fails the aggregation job.
                try:
                    from genesis.eval.regression_alert import (
                        check_and_alert_regressions,
                    )

                    regressions = await check_and_alert_regressions(
                        rt._db,
                        getattr(rt, "_outreach_pipeline", None),
                    )
                    if regressions:
                        logger.info(
                            "J9 regression check surfaced %d regression(s)",
                            len(regressions),
                        )
                except Exception:
                    logger.warning("J9 regression check failed", exc_info=True)
                rt.record_job_success("j9_eval_aggregation")
            except Exception as exc:
                rt.record_job_failure("j9_eval_aggregation", str(exc))
                logger.exception("J9 weekly aggregation failed")

        rt._learning_scheduler.add_job(
            _run_j9_eval_aggregation,
            CronTrigger(
                day_of_week="sun", hour=7, minute=30, timezone=user_timezone()
            ),  # :30 to avoid schedule_analytical at 7:00
            id="j9_eval_aggregation",
            max_instances=1,
            misfire_grace_time=3600,
        )

        # PR-review findings harvest (Sundays 6:45am — 45 min BEFORE the
        # 07:30 j9_eval_aggregation reads the pr_review_findings rows, and
        # in the SAME timezone so the ordering can't invert across DST).
        async def _run_pr_review_harvest():
            try:
                from genesis.eval.pr_review_harvest import (
                    harvest_pr_review_findings,
                )

                summary = await harvest_pr_review_findings(rt._db)
                if summary.get("error"):
                    # Error-dict return (repo-resolve / pr-list failure):
                    # the harvest didn't raise, but the job did no work —
                    # surface it as a job failure, not a false green.
                    raise RuntimeError(str(summary["error"]))
                logger.info(
                    "PR review harvest: %d PRs, %d findings, %d errors",
                    summary.get("prs_seen", 0),
                    summary.get("findings_total", 0),
                    len(summary.get("errors") or []),
                )
                rt.record_job_success("pr_review_harvest")
            except Exception as exc:
                rt.record_job_failure("pr_review_harvest", str(exc))
                logger.exception("PR review harvest failed")

        rt._learning_scheduler.add_job(
            _run_pr_review_harvest,
            CronTrigger(day_of_week="sun", hour=6, minute=45, timezone=user_timezone()),
            id="pr_review_harvest",
            max_instances=1,
            misfire_grace_time=3600,
        )

        # Model-roster gauntlet (weekly, Sat 5am). Validates each runnable roster
        # member can still drive CC through a coding fix-loop; a PASS->FAIL
        # regression surfaces an advisory BLOCKER alert + human-gated proposal
        # (NEVER auto-removes a model). OFF by default (spends inference on paid
        # peers) — gated on roster `gauntlet.scheduled`; manual CLI always works.
        async def _run_model_gauntlet() -> None:
            try:
                if rt.paused:
                    logger.debug("Model gauntlet skipped (Genesis paused)")
                    return
            except Exception:
                logger.warning("Pause check failed — skipping model gauntlet", exc_info=True)
                return
            try:
                from genesis.cc.roster import RosterError, load_roster
                from genesis.eval.gauntlet import GauntletBusyError, run_gauntlet
                from genesis.eval.gauntlet_regression import check_gauntlet_regression
                from genesis.eval.types import EvalTrigger

                roster_cfg = load_roster()
                if not (roster_cfg.get("gauntlet") or {}).get("scheduled", False):
                    logger.debug("Model gauntlet: scheduled auto-run disabled")
                    rt.record_job_success("model_gauntlet")
                    return

                models = list((roster_cfg.get("models") or {}).keys())
                ran = 0
                for model in models:
                    try:
                        summary = await run_gauntlet(
                            model,
                            db=rt._db,
                            trigger=EvalTrigger.SCHEDULE,
                        )
                    except RosterError:
                        logger.info(
                            "gauntlet: %s not runnable (unconfigured/keyless) — skipping",
                            model,
                        )
                        continue
                    except GauntletBusyError:
                        logger.info("gauntlet: %s already running — skipping", model)
                        continue
                    ran += 1
                    try:
                        await check_gauntlet_regression(
                            rt._db,
                            summary,
                            getattr(rt, "_outreach_pipeline", None),
                        )
                    except Exception:
                        logger.warning(
                            "gauntlet regression check failed for %s",
                            model,
                            exc_info=True,
                        )
                logger.info("Model gauntlet: ran %d/%d roster model(s)", ran, len(models))
                rt.record_job_success("model_gauntlet")
            except Exception as exc:
                rt.record_job_failure("model_gauntlet", str(exc))
                logger.exception("Model gauntlet job failed")

        rt._learning_scheduler.add_job(
            _run_model_gauntlet,
            CronTrigger(day_of_week="sat", hour=5, minute=0, timezone=user_timezone()),
            id="model_gauntlet",
            max_instances=1,
            misfire_grace_time=3600,
        )

        rt._learning_scheduler.start()
        logger.info("Genesis learning scheduler started")

    except ImportError:
        logger.warning("genesis.learning not available")
    except Exception as exc:
        logger.exception("Failed to initialize learning")
        from genesis.runtime._degradation import record_init_degradation

        await record_init_degradation(
            rt._db, rt._event_bus, "learning", "learning_scheduler", str(exc), severity="error"
        )
