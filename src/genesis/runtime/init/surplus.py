"""Init function: _init_surplus."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")

_CONFIG_DIR = __import__("pathlib").Path(__file__).resolve().parents[3] / "config"


def _load_surplus_config() -> dict:
    """Load surplus config from YAML with .local.yaml overlay."""
    from pathlib import Path

    import yaml

    from genesis._config_overlay import merge_local_overlay

    config_path = _CONFIG_DIR / "surplus.yaml"
    if not config_path.exists():
        logger.info("Surplus config not found at %s — using defaults", config_path)
        return {}
    try:
        raw = yaml.safe_load(Path(config_path).read_text()) or {}
        return merge_local_overlay(raw, config_path)
    except Exception:
        logger.error("Failed to read surplus config from %s", config_path, exc_info=True)
        return {}


async def _degraded(rt: GenesisRuntime, component: str, error: str = "failed to wire") -> None:
    """Record a surplus init degradation."""
    from genesis.runtime._degradation import record_init_degradation

    await record_init_degradation(rt._db, rt._event_bus, "surplus", component, error)


async def init(rt: GenesisRuntime) -> None:
    """Initialize surplus compute scheduler, executors, and scheduled jobs."""
    if rt._db is None:
        return
    try:
        from genesis.surplus.compute_availability import ComputeAvailability
        from genesis.surplus.idle_detector import IdleDetector
        from genesis.surplus.queue import SurplusQueue
        from genesis.surplus.scheduler import SurplusScheduler

        # Load surplus config (base + .local.yaml overlay from dashboard)
        cfg = _load_surplus_config()
        dispatch = cfg.get("dispatch", {})
        jobs = cfg.get("jobs", {})

        queue = SurplusQueue(db=rt._db)
        rt._surplus_queue = queue
        idle_detector = IdleDetector()
        rt._idle_detector = idle_detector
        compute = ComputeAvailability()

        rt._surplus_scheduler = SurplusScheduler(
            db=rt._db,
            queue=queue,
            idle_detector=idle_detector,
            compute_availability=compute,
            event_bus=rt._event_bus,
            enable_code_audits=False,
            dispatch_interval_minutes=int(dispatch.get("interval_minutes", 5)),
            brainstorm_check_hours=int(jobs.get("brainstorm_check_hours", 12)),
            task_expiry_hours=int(dispatch.get("task_expiry_hours", 72)),
            code_audit_hours=int(jobs.get("code_audit_hours", 12)),
            code_index_hours=int(jobs.get("code_index_hours", 4)),
            recon_gather_hours=int(jobs.get("recon_gather_hours", 84)),
            maintenance_hours=int(jobs.get("maintenance_hours", 6)),
            analytical_hours=int(jobs.get("analytical_hours", 24)),
            follow_up_dispatch_minutes=int(jobs.get("follow_up_dispatch_minutes", 5)),
            memory_extraction_hours=int(jobs.get("memory_extraction_hours", 2)),
        )

        try:
            from genesis.recon.gatherer import ReconGatherer
            recon_gatherer = ReconGatherer(db=rt._db)
            rt._surplus_scheduler.set_recon_gatherer(recon_gatherer)
            logger.info("ReconGatherer wired to surplus scheduler")
        except (ImportError, AttributeError):
            logger.error("Failed to wire ReconGatherer", exc_info=True)
            await _degraded(rt, "ReconGatherer")
        except Exception:
            logger.error("Unexpected error wiring ReconGatherer", exc_info=True)
            await _degraded(rt, "ReconGatherer")

        await rt._surplus_scheduler.start()
        logger.info("Genesis surplus scheduler started")

        if rt._router is not None and rt._db is not None:
            from genesis.surplus.executor import SurplusLLMExecutor

            real_executor = SurplusLLMExecutor(
                rt._router, db=rt._db,
            )
            rt._surplus_scheduler.set_executor(real_executor)
            logger.info("Surplus executor upgraded: SurplusLLMExecutor")

        try:
            from genesis.surplus.code_index import CodeIndexExecutor
            code_index_executor = CodeIndexExecutor(db=rt._db)
            rt._surplus_scheduler.set_code_index_executor(code_index_executor)
            logger.info("CodeIndexExecutor wired to surplus scheduler")
        except (ImportError, AttributeError):
            logger.error("Failed to wire CodeIndexExecutor", exc_info=True)
            await _degraded(rt, "CodeIndexExecutor")
        except Exception:
            logger.error("Unexpected error wiring CodeIndexExecutor", exc_info=True)
            await _degraded(rt, "CodeIndexExecutor")

        try:
            from genesis.surplus.code_audit import CodeAuditExecutor
            if rt._router is not None:
                code_audit_executor = CodeAuditExecutor(
                    router=rt._router,
                    db=rt._db,
                )
                rt._surplus_scheduler.set_code_audit_executor(code_audit_executor)
                logger.info("CodeAuditExecutor wired to surplus scheduler")
        except (ImportError, AttributeError):
            logger.error("Failed to wire CodeAuditExecutor", exc_info=True)
            await _degraded(rt, "CodeAuditExecutor")
        except Exception:
            logger.error("Unexpected error wiring CodeAuditExecutor", exc_info=True)
            await _degraded(rt, "CodeAuditExecutor")

        try:
            from genesis.bookmark.enrichment import BookmarkEnrichmentExecutor
            from genesis.bookmark.manager import BookmarkManager

            if rt._memory_store is not None and rt._hybrid_retriever is not None:
                bm_mgr = BookmarkManager(
                    memory_store=rt._memory_store,
                    hybrid_retriever=rt._hybrid_retriever,
                    db=rt._db,
                    surplus_queue=rt._surplus_scheduler._queue,
                )
                enrichment_executor = BookmarkEnrichmentExecutor(
                    bookmark_manager=bm_mgr,
                    db=rt._db,
                    router=rt._router,
                )
                rt._surplus_scheduler.set_bookmark_enrichment_executor(enrichment_executor)
                logger.info("BookmarkEnrichmentExecutor wired to surplus scheduler")
        except (ImportError, AttributeError):
            logger.error("Failed to wire BookmarkEnrichmentExecutor", exc_info=True)
            await _degraded(rt, "BookmarkEnrichmentExecutor")
        except Exception:
            logger.error("Unexpected error wiring BookmarkEnrichmentExecutor", exc_info=True)
            await _degraded(rt, "BookmarkEnrichmentExecutor")

        try:
            from genesis.eval.surplus_executor import ModelEvalExecutor
            model_eval_executor = ModelEvalExecutor(db=rt._db)
            rt._surplus_scheduler.set_model_eval_executor(model_eval_executor)
            logger.info("ModelEvalExecutor wired to surplus scheduler")
        except (ImportError, AttributeError):
            logger.error("Failed to wire ModelEvalExecutor", exc_info=True)
            await _degraded(rt, "ModelEvalExecutor")
        except Exception:
            logger.error("Unexpected error wiring ModelEvalExecutor", exc_info=True)
            await _degraded(rt, "ModelEvalExecutor")

        # J-9 eval batch executor (daily memory relevance scoring)
        try:
            from genesis.eval.j9_batch import J9EvalBatchExecutor
            j9_executor = J9EvalBatchExecutor(db=rt._db)
            rt._surplus_scheduler.set_j9_eval_batch_executor(j9_executor)
            logger.info("J9EvalBatchExecutor wired to surplus scheduler")
        except (ImportError, AttributeError):
            logger.error("Failed to wire J9EvalBatchExecutor", exc_info=True)
            await _degraded(rt, "J9EvalBatchExecutor")
        except Exception:
            logger.error("Unexpected error wiring J9EvalBatchExecutor", exc_info=True)
            await _degraded(rt, "J9EvalBatchExecutor")

        try:
            from genesis.surplus.maintenance import (
                BackupVerificationExecutor,
                DbMaintenanceExecutor,
                DeadLetterReplayExecutor,
                DiskCleanupExecutor,
            )
            maint_kwargs: dict = {
                "disk_cleanup": DiskCleanupExecutor(),
                "backup_verification": BackupVerificationExecutor(),
                "db_maintenance": DbMaintenanceExecutor(db=rt._db),
            }
            # DLQ replay needs both dead_letter_queue and router
            if rt._dead_letter_queue is not None and rt._router is not None:
                maint_kwargs["dead_letter_replay"] = DeadLetterReplayExecutor(
                    dead_letter=rt._dead_letter_queue, router=rt._router,
                )
            rt._surplus_scheduler.set_maintenance_executors(**maint_kwargs)
            logger.info("Maintenance executors wired to surplus scheduler")
        except (ImportError, AttributeError):
            logger.error("Failed to wire maintenance executors", exc_info=True)
            await _degraded(rt, "MaintenanceExecutors")
        except Exception:
            logger.error("Unexpected error wiring maintenance executors", exc_info=True)
            await _degraded(rt, "MaintenanceExecutors")

        try:
            from genesis.follow_ups.dispatcher import FollowUpDispatcher
            follow_up_dispatcher = FollowUpDispatcher(db=rt._db, queue=queue)
            rt._surplus_scheduler.set_follow_up_dispatcher(follow_up_dispatcher)
            logger.info("FollowUpDispatcher wired to surplus scheduler")
        except (ImportError, AttributeError):
            logger.error("Failed to wire FollowUpDispatcher", exc_info=True)
            await _degraded(rt, "FollowUpDispatcher")
        except Exception:
            logger.error("Unexpected error wiring FollowUpDispatcher", exc_info=True)
            await _degraded(rt, "FollowUpDispatcher")

        try:
            from genesis.surplus.findings_bridge import FindingsBridge
            rt._findings_bridge = FindingsBridge(db=rt._db)
            logger.info("FindingsBridge initialized")
        except (ImportError, AttributeError):
            logger.error("Failed to wire FindingsBridge", exc_info=True)
            rt._findings_bridge = None
        except Exception:
            logger.error("Unexpected error wiring FindingsBridge", exc_info=True)
            rt._findings_bridge = None

        try:
            if rt._memory_store is not None and rt._router is not None:
                rt._surplus_scheduler.set_extraction_deps(
                    store=rt._memory_store, router=rt._router,
                )
                from apscheduler.triggers.interval import (
                    IntervalTrigger as _ExtIvlTrig,
                )
                extraction_hours = rt._surplus_scheduler._memory_extraction_hours
                rt._surplus_scheduler._scheduler.add_job(
                    rt._surplus_scheduler.run_memory_extraction,
                    _ExtIvlTrig(hours=extraction_hours),
                    id="memory_extraction",
                    max_instances=1,
                    misfire_grace_time=300,
                )
                logger.info("Memory extraction job wired (%dh interval)", extraction_hours)
        except (ImportError, AttributeError):
            logger.error("Failed to wire memory extraction", exc_info=True)
            await _degraded(rt, "memory_extraction")
        except Exception:
            logger.error("Unexpected error wiring memory extraction", exc_info=True)
            await _degraded(rt, "memory_extraction")

        try:
            from apscheduler.triggers.cron import CronTrigger as _CalibCron

            from genesis.surplus.extraction_calibration import (
                run_calibration as _run_cal,
            )

            async def _calibration_job() -> None:
                try:
                    summary = await _run_cal(rt._db)
                    logger.info(
                        "Extraction calibration: %d extracted, %d retrieved",
                        summary["total_extracted"], summary["total_retrieved"],
                    )
                    rt.record_job_success("extraction_calibration")
                except Exception as exc:
                    logger.exception("Extraction calibration failed")
                    rt.record_job_failure("extraction_calibration", str(exc))

            rt._surplus_scheduler._scheduler.add_job(
                _calibration_job,
                _CalibCron(day_of_week="sun", hour=12, minute=0),
                id="extraction_calibration",
                max_instances=1,
                misfire_grace_time=3600,
            )
            logger.info("Extraction calibration job wired (Sunday 12:00 UTC)")
        except (ImportError, AttributeError):
            logger.error("Failed to wire extraction calibration", exc_info=True)
        except Exception:
            logger.error("Unexpected error wiring extraction calibration", exc_info=True)

        if rt._pipeline_orchestrator is not None:
            try:
                from apscheduler.triggers.interval import (
                    IntervalTrigger as _IvlTrig,
                )

                from genesis.pipeline.profiles import ProfileLoader

                loader = ProfileLoader()
                loader.load_all()
                profiles = loader.merge_overlay()
                for name, profile in profiles.items():
                    if not profile.enabled:
                        continue

                    # Skip if owning module is disabled
                    if rt._module_registry is not None:
                        skip_profile = False
                        for mod_name in rt._module_registry.list_modules():
                            mod = rt._module_registry.get(mod_name)
                            if mod and mod.get_research_profile_name() == name and not mod.enabled:
                                logger.info(
                                    "Skipping profile %s — owning module %s is disabled",
                                    name, mod_name,
                                )
                                skip_profile = True
                                break
                        if skip_profile:
                            continue

                    async def _cycle(pname: str = name) -> None:
                        from genesis.runtime.init.pipeline import run_pipeline_cycle
                        await run_pipeline_cycle(rt, pname)

                    rt._surplus_scheduler._scheduler.add_job(
                        _cycle,
                        _IvlTrig(minutes=profile.tier0_interval_minutes),
                        id=f"pipeline_{name}",
                        max_instances=1,
                        misfire_grace_time=300,
                        replace_existing=True,
                    )
                    logger.info(
                        "Scheduled pipeline cycle: %s every %dm",
                        name,
                        profile.tier0_interval_minutes,
                    )
            except (ValueError, KeyError):
                logger.error("Config error scheduling pipeline cycles", exc_info=True)
            except Exception:
                logger.error("Failed to schedule pipeline cycles", exc_info=True)
    except ImportError:
        logger.warning("genesis.surplus not available")
    except Exception:
        logger.exception("Failed to initialize surplus scheduler")
