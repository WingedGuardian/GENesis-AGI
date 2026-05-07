"""Init function: _init_learning."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.env import cc_project_dir

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize learning pipeline, triage, calibration, harvest, and all scheduled jobs."""
    if rt._db is None or rt._router is None:
        logger.warning(
            "Learning skipped — missing prerequisites "
            "(db=%s, router=%s)",
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

            probes = [
                partial(probe_db, rt._db),
                probe_qdrant,
                probe_ollama,
            ]

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
                logger.warning("Pause check failed — skipping calibration as precaution", exc_info=True)
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
            CronTrigger(hour=3, minute=0),
            id="triage_calibration_daily",
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
                memory_dir = (
                    Path.home()
                    / ".claude"
                    / "projects"
                    / cc_project_dir()
                    / "memory"
                )
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
                        len(items), stored, skipped,
                    )
                rt.record_job_success("auto_memory_harvest")
            except Exception as exc:
                rt.record_job_failure("auto_memory_harvest", str(exc))
                logger.exception("Auto-memory harvest failed")

        rt._learning_scheduler.add_job(
            _harvest_and_store,
            CronTrigger(hour="*/6", minute=15),
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
                        count, stale,
                    )
            except Exception as exc:
                rt.record_job_failure("observation_expiry_sweep", str(exc))
                logger.exception("Observation expiry sweep failed")

        rt._learning_scheduler.add_job(
            _observation_expiry_sweep,
            CronTrigger(hour=2, minute=0),
            id="observation_expiry_sweep",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _run_recovery() -> None:
            if rt._recovery_orchestrator is not None:
                try:
                    report = await rt._recovery_orchestrator.run_recovery()
                    rt.record_job_success("recovery_orchestrator")
                    if (report.dead_letters_replayed or report.embeddings_recovered
                            or report.items_drained):
                        logger.info(
                            "Recovery: replayed=%d embeddings=%d drained=%d",
                            report.dead_letters_replayed,
                            report.embeddings_recovered,
                            report.items_drained,
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
            CronTrigger(hour=1, minute=30),
            id="dead_letter_expiry",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _expire_stale_messages() -> None:
            try:
                from genesis.db.crud import message_queue
                now = datetime.now(UTC).isoformat()
                expired = await message_queue.expire_older_than(
                    rt._db, max_age_hours=168, expired_at=now,
                )
                rt.record_job_success("message_queue_expiry")
                if expired:
                    logger.info("Expired %d stale message queue items (>7d)", expired)
            except Exception as exc:
                rt.record_job_failure("message_queue_expiry", str(exc))
                logger.exception("Message queue expiry failed")

        rt._learning_scheduler.add_job(
            _expire_stale_messages,
            CronTrigger(hour=2, minute=30),
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
                logger.warning("Pause check failed — skipping redispatch as precaution", exc_info=True)
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
                            ok, fail,
                        )
                except Exception:
                    rt.record_job_failure(
                        "dead_letter_redispatch", "redispatch failed",
                    )
                    logger.exception("Dead letter redispatch failed")

        rt._learning_scheduler.add_job(
            _redispatch_dead_letters,
            CronTrigger(hour="0,6,12,18", minute=45),
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

        rt._learning_scheduler.add_job(
            _run_procedure_promotion,
            IntervalTrigger(hours=1),
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
                            "identity_loader not available, skipping "
                            "USER_KNOWLEDGE.md synthesis",
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
                            identity_loader.write_user_knowledge_md(
                                result.model,
                                evidence_count=result.evidence_count,
                                narrative=narrative,
                            )
                            if narrative:
                                logger.info(
                                    "USER_KNOWLEDGE.md updated via LLM "
                                    "synthesis (call site 11)",
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
            CronTrigger(hour=4, minute=30),
            id="user_model_evolution",
            max_instances=1,
            misfire_grace_time=3600,
        )

        async def _reap_stale_sessions() -> None:
            if rt._db is None:
                return
            try:
                from genesis.db.crud.cc_sessions import reap_stale

                cutoff = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
                reaped = await reap_stale(rt._db, older_than=cutoff)
                rt.record_job_success("session_reaper")
                if reaped:
                    logger.info("Session reaper: marked %d stale sessions as completed", reaped)
            except Exception as exc:
                rt.record_job_failure("session_reaper", str(exc))
                logger.exception("Session reaper failed")

        rt._learning_scheduler.add_job(
            _reap_stale_sessions,
            CronTrigger(hour="1,7,13,19", minute=30),
            id="session_reaper",
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
            CronTrigger(hour="2,8,14,20", minute=0),
            id="activity_log_reaper",
            max_instances=1,
            misfire_grace_time=3600,
        )

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
                    "pgrep", "-P", str(pid),
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if not stdout.strip():
                    return []
                children = [
                    int(p.strip()) for p in stdout.decode().strip().split("\n")
                    if p.strip()
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
                        "pgrep", flag, pattern,
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
                            after_comm = raw[raw.rfind(")") + 2:]
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
                                content=json.dumps({
                                    "killed_count": len(all_killed),
                                    "processes": [
                                        {"pid": p, "label": lbl}
                                        for p, lbl in all_killed
                                    ],
                                }),
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

        rt._learning_scheduler.add_job(
            _reap_stale_processes,
            IntervalTrigger(hours=1),
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
                    db=rt._db, router=rt._router, outreach_fn=outreach_fn,
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
            CronTrigger(day_of_week="sun", hour=4, minute=0),
            id="skill_evolution",
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
        await record_init_degradation(rt._db, rt._event_bus, "learning", "learning_scheduler", str(exc), severity="error")
