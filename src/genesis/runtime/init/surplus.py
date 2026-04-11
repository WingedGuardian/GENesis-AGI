"""Init function: _init_surplus."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


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

        if rt._reflection_engine is not None and rt._db is not None:
            from genesis.surplus.executor import ReflectionBasedSurplusExecutor

            real_executor = ReflectionBasedSurplusExecutor(
                rt._reflection_engine, db=rt._db,
            )
            rt._surplus_scheduler.set_executor(real_executor)
            logger.info("Surplus executor upgraded: ReflectionBasedSurplusExecutor")

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
                rt._surplus_scheduler._scheduler.add_job(
                    rt._surplus_scheduler.run_memory_extraction,
                    _ExtIvlTrig(hours=2),
                    id="memory_extraction",
                    max_instances=1,
                    misfire_grace_time=300,
                )
                logger.info("Memory extraction job wired (2h interval)")
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
                profiles = loader.load_all()
                for name, profile in profiles.items():
                    if not profile.enabled:
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
