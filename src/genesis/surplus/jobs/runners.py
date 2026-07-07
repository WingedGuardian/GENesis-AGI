"""Direct-run jobs — scheduled work delegating to wired components.

Bodies extracted verbatim from ``SurplusScheduler``; the scheduler keeps every
original method name as a thin delegate. Function-scope imports are
intentional — they are both the tests' patch-target seam and the import-cycle
breaker; do not hoist them to module top.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.observability.types import Severity, Subsystem

if TYPE_CHECKING:
    from genesis.surplus.jobs.context import SchedulerContext

logger = logging.getLogger(__name__)


async def dispatch_follow_ups(sched: SchedulerContext) -> None:
    """Run the follow-up dispatcher cycle (always-on, not idle-gated)."""
    if sched._follow_up_dispatcher is None:
        return
    try:
        from genesis.runtime import GenesisRuntime
        if GenesisRuntime.instance().paused:
            logger.debug("Follow-up dispatch skipped (Genesis paused)")
            return
    except Exception:
        pass
    try:
        summary = await sched._follow_up_dispatcher.run_cycle()
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


async def run_recon_gather(sched: SchedulerContext) -> None:
    """Check watchlist projects for new GitHub releases and star counts."""
    if sched._recon_gatherer is None:
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("recon_gather", "gatherer not wired")
        except Exception:
            pass
        return
    try:
        result = await sched._recon_gatherer.gather_releases()
        if result.new_findings > 0:
            logger.info(
                "Recon gather found %d new release(s): %s",
                result.new_findings, "; ".join(result.details),
            )
        try:
            star_result = await sched._recon_gatherer.gather_stars()
            if star_result.new_findings > 0:
                logger.info(
                    "Star gather found %d change(s): %s",
                    star_result.new_findings, "; ".join(star_result.details),
                )
        except Exception:
            logger.exception("Star gather failed (releases unaffected)")
        if sched._event_bus:
            await sched._event_bus.emit(
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
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.RECON, Severity.ERROR,
                "recon_gather.failed",
                "Recon gather failed with exception",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("recon_gather", str(exc))
        except Exception:
            pass


async def run_model_intelligence(sched: SchedulerContext) -> None:
    """Run model intelligence scan (weekly)."""
    if sched._model_intelligence_job is None:
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure(
                "model_intelligence", "job not wired",
            )
        except Exception:
            pass
        return
    try:
        result = await sched._model_intelligence_job.run()
        total = result.get("total_findings", 0)
        logger.info("Model intelligence scan: %d findings", total)
        if sched._event_bus:
            await sched._event_bus.emit(
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
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.RECON, Severity.ERROR,
                "model_intelligence.failed",
                "Model intelligence scan failed",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("model_intelligence", str(exc))
        except Exception:
            pass


async def run_skill_security_scan(sched: SchedulerContext) -> None:
    """Run the weekly skill-security scan (SkillSpector → recon findings)."""
    if sched._skill_security_scan_job is None:
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure(
                "skill_security_scan", "job not wired",
            )
        except Exception:
            pass
        return
    try:
        result = await sched._skill_security_scan_job.run()
        total = result.get("total_findings", 0)
        logger.info("Skill-security scan: %d untrusted findings", total)
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.RECON, Severity.DEBUG,
                "heartbeat", "skill_security_scan completed",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_success("skill_security_scan")
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Skill-security scan failed")
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.RECON, Severity.ERROR,
                "skill_security_scan.failed",
                "Skill-security scan failed",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("skill_security_scan", str(exc))
        except Exception:
            pass


async def run_github_discovery(sched: SchedulerContext) -> None:
    """Run weekly curated GitHub Discovery (new repos → recon triage queue)."""
    try:
        from genesis.runtime import GenesisRuntime
        if GenesisRuntime.instance().paused:
            logger.debug("GitHub Discovery skipped (Genesis paused)")
            return
    except Exception:
        pass
    if sched._github_discovery_job is None:
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure(
                "github_discovery", "job not wired",
            )
        except Exception:
            pass
        return
    try:
        result = await sched._github_discovery_job.run()
        filed = result.get("filed", 0)
        logger.info("GitHub Discovery: %d new repo(s) filed for triage", filed)
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.RECON, Severity.DEBUG,
                "heartbeat", "github_discovery completed",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_success("github_discovery")
        except Exception:
            pass
    except Exception as exc:
        logger.exception("GitHub Discovery failed")
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.RECON, Severity.ERROR,
                "github_discovery.failed",
                "GitHub Discovery failed",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("github_discovery", str(exc))
        except Exception:
            pass


async def run_models_md_synthesis(sched: SchedulerContext) -> None:
    """Run weekly models.md synthesis (Sunday 8am UTC).

    Dispatches a CC background session to update docs/reference/models.md
    from recent model intelligence findings.  Fire-and-forget: job health
    records the dispatch outcome, not the session completion.
    """
    try:
        from genesis.runtime import GenesisRuntime
        if GenesisRuntime.instance().paused:
            logger.debug("Models.md synthesis skipped (Genesis paused)")
            return
    except Exception:
        pass
    if sched._models_md_synthesis_job is None:
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure(
                "models_md_synthesis", "job not wired",
            )
        except Exception:
            pass
        return
    try:
        result = await sched._models_md_synthesis_job.run()
        skipped = result.get("skipped", False)
        if skipped:
            logger.info("Models.md synthesis skipped: %s", result.get("reason"))
        else:
            logger.info(
                "Models.md synthesis dispatched: %d findings (session=%s)",
                result.get("findings_count", 0),
                result.get("session_id", "?"),
            )
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.RECON, Severity.DEBUG,
                "heartbeat", "models_md_synthesis dispatched",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_success("models_md_synthesis")
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Models.md synthesis failed")
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.RECON, Severity.ERROR,
                "models_md_synthesis.failed",
                "Models.md synthesis failed",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("models_md_synthesis", str(exc))
        except Exception:
            pass


async def run_db_integrity_check(sched: SchedulerContext) -> None:
    """Weekly full PRAGMA integrity_check with an alarm on corruption.

    Deterministic counterpart to the DbMaintenanceExecutor's fast
    quick_check — guaranteed cadence plus a real alarm (observation +
    ERROR event) so DB corruption can't go silent.
    """
    try:
        from genesis.runtime import GenesisRuntime
        if GenesisRuntime.instance().paused:
            logger.debug("DB integrity check skipped (Genesis paused)")
            return
    except Exception:
        pass
    try:
        from genesis.surplus.maintenance import check_db_integrity
        status = await check_db_integrity(sched._db)
        if status == "ok":
            logger.info("Weekly DB integrity check passed")
        else:
            logger.error("DB integrity check FAILED: %s", status[:500])
            await sched._alarm_db_integrity(status)
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_success("db_integrity_check")
        except Exception:
            pass
    except Exception as exc:
        logger.exception("DB integrity check job failed")
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("db_integrity_check", str(exc))
        except Exception:
            pass


async def alarm_db_integrity(sched: SchedulerContext, detail: str) -> None:
    """Persist + broadcast a DB-corruption alarm (observation + ERROR event)."""
    import uuid

    # Observation — surfaces in the morning report / health views.
    # skip_if_duplicate so a persistent corruption doesn't re-alarm weekly.
    try:
        from genesis.db.crud import observations
        await observations.create(
            sched._db,
            id=uuid.uuid4().hex,
            source="surplus_scheduler",
            type="db_integrity_failure",
            content=f"PRAGMA integrity_check failed: {detail[:1000]}",
            priority="critical",
            created_at=datetime.now(UTC).isoformat(),
            skip_if_duplicate=True,
        )
    except Exception:
        logger.warning("Failed to write db_integrity_failure observation", exc_info=True)

    # Event bus — dashboard / Sentinel alerting path.
    if sched._event_bus:
        try:
            await sched._event_bus.emit(
                Subsystem.SURPLUS, Severity.ERROR,
                "db.integrity_failed",
                f"SQLite integrity_check failed: {detail[:300]}",
            )
        except Exception:
            logger.warning("Failed to emit db integrity event", exc_info=True)


async def run_memory_extraction(sched: SchedulerContext) -> None:
    """Run periodic memory extraction from session transcripts."""
    try:
        from genesis.runtime import GenesisRuntime
        if GenesisRuntime.instance().paused:
            logger.debug("Memory extraction skipped (Genesis paused)")
            return
    except Exception:
        logger.warning("Pause check failed — skipping extraction as precaution", exc_info=True)
        return
    if sched._extraction_store is None or sched._extraction_router is None:
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
        linker = sched._extraction_store.linker
        summary = await run_extraction_cycle(
            db=sched._db,
            store=sched._extraction_store,
            router=sched._extraction_router,
            linker=linker,
        )
        logger.info(
            "Memory extraction completed: %d sessions, %d entities, %d errors",
            summary["sessions_processed"],
            summary["entities_extracted"],
            summary["errors"],
        )
        if sched._event_bus:
            await sched._event_bus.emit(
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
        if sched._event_bus:
            await sched._event_bus.emit(
                Subsystem.SURPLUS, Severity.ERROR,
                "memory_extraction.failed",
                "Memory extraction failed with exception",
            )
        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("memory_extraction", str(exc))
        except Exception:
            pass
