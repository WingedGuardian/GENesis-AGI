"""The dream-cycle pair — weekly clustering plus the daily synthesis drain.

Bodies extracted verbatim from ``SurplusScheduler``; the scheduler keeps both
method names as thin delegates. These jobs read no scheduler state — every
dependency comes from ``GenesisRuntime.instance()`` — so they take no
arguments. The two jobs share the runtime's ``_heavy_workload`` flag and MUST
stay together (see the owner-checked ``finally`` blocks). Function-scope
imports are intentional — they are both the tests' patch-target seam and the
import-cycle breaker; do not hoist them to module top.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


async def run_dream_cycle() -> None:
    """Run the WEEKLY dream-cycle clustering pass (Sunday 4am).

    Scans + clusters episodic memory, runs the additive link/centrality
    layer, and persists the value-ranked synthesis worklist that
    ``run_dream_synthesis_drain`` consumes daily. Destructive phases
    (entity resolution, link repair) are dry-run by default — set the
    ``GENESIS_DREAM_CYCLE_LIVE=1`` environment variable (there is NO
    config-file key for this) after reviewing dry-run reports.
    """
    try:
        from genesis.runtime import GenesisRuntime

        if GenesisRuntime.instance().paused:
            logger.debug("Dream cycle skipped (Genesis paused)")
            return
    except Exception:
        logger.warning("Pause check failed — skipping dream cycle", exc_info=True)
        return

    # Record start so crashes mid-execution are visible in job_health.
    # The June 1 crash left last_run at May 17 because neither
    # record_job_success nor record_job_failure was reached.
    try:
        from genesis.runtime import GenesisRuntime

        GenesisRuntime.instance().record_job_start("dream_cycle")
    except Exception:
        pass  # Don't let health tracking prevent the actual job

    try:
        from genesis.memory import dream_cycle
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        store = rt.memory_store
        if rt.db is None or store is None or rt.router is None:
            logger.warning("Dream cycle skipped — missing runtime dependencies")
            return

        # MemoryStore always holds the QdrantClient it was constructed with.
        qdrant = store.qdrant_client
        if qdrant is None:
            logger.warning("Dream cycle skipped — MemoryStore has no Qdrant client")
            return

        # Signal heavy workload so Sentinel and watchdog defer restarts.
        rt._heavy_workload = "dream_cycle"
        rt._heavy_workload_since = datetime.now(UTC)

        # Default dry-run until user enables live mode.
        # Set GENESIS_DREAM_CYCLE_LIVE=1 to enable actual merges.
        import os

        dry_run = os.environ.get("GENESIS_DREAM_CYCLE_LIVE", "") not in ("1", "true")

        report = await dream_cycle.run(
            qdrant=qdrant,
            db=rt.db,
            router=rt.router,
            store=store,
            dry_run=dry_run,
        )

        # Write observation with the report
        try:
            import uuid as _uuid  # noqa: PLC0415

            from genesis.db.crud import observations as obs_crud

            await obs_crud.create(
                rt.db,
                id=str(_uuid.uuid4()),
                source="dream_cycle",
                type="dream_cycle_report",
                content=(
                    f"Dream cycle {'DRY RUN' if dry_run else 'LIVE'} "
                    f"(weekly clustering): "
                    f"{report.get('clusters_found', 0)} clusters found, "
                    f"{report.get('worklist_enqueued', 0)} enqueued for "
                    f"daily drain, "
                    f"{report.get('oversize_flagged', 0)} oversize flagged, "
                    f"{len(report.get('errors', []))} errors"
                ),
                priority="low",
                created_at=datetime.now(UTC).isoformat(),
            )
        except Exception:
            pass

        logger.info("Dream cycle complete: %s", report)
        with contextlib.suppress(Exception):
            GenesisRuntime.instance().record_job_success("dream_cycle")
    except Exception as exc:
        logger.exception("Dream cycle failed: %s", exc)
        try:
            from genesis.runtime import GenesisRuntime

            GenesisRuntime.instance().record_job_failure(
                "dream_cycle",
                exc=exc,
            )
        except Exception as rec_err:
            logger.error(
                "Failed to record dream_cycle failure: %s (original error: %s)",
                rec_err,
                exc,
            )
    finally:
        # Always clear heavy workload flag, even on failure — but ONLY if
        # this job set it: an early return above fires before the flag is
        # set, and an unconditional clear would clobber a flag held by the
        # daily synthesis drain (the two dream jobs share the flag).
        # Use the captured `rt` reference from the try block above —
        # re-looking up GenesisRuntime.instance() here introduces a
        # second failure mode during shutdown races.  If `rt` is
        # unbound (import/lookup failed), NameError is caught below.
        try:
            if rt._heavy_workload == "dream_cycle":
                rt._heavy_workload = None
                rt._heavy_workload_since = None
        except Exception:
            pass


async def run_dream_synthesis_drain() -> None:
    """Drain a bounded, value-ranked slice of the dream-cycle synthesis
    worklist (DAILY 8am — the weekly clustering job persists the worklist).

    SHADOW mode: exercises the full queue + rehydration lifecycle but makes
    no LLM calls and no memory mutations, reporting what it WOULD merge.
    The live flip (honoring ``GENESIS_DREAM_CYCLE_LIVE``) is a separate,
    user-gated change (T2-D PR2).
    """
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if rt.paused:
            logger.debug("Dream synthesis drain skipped (Genesis paused)")
            return
        if rt.heavy_workload:
            # Weekly clustering (or another batch job) still running —
            # don't overlap; today's slice re-surfaces tomorrow.
            logger.info(
                "Dream synthesis drain skipped — heavy workload active (%s)",
                rt.heavy_workload,
            )
            return
        # Dependency checks BEFORE record_job_start — a skip on a
        # misconfigured deploy must not leave the job perpetually
        # "started" in job_health (masks real stuck-job detection).
        store = rt.memory_store
        if rt.db is None or store is None or rt.router is None:
            logger.warning("Dream synthesis drain skipped — missing runtime dependencies")
            return
        qdrant = store.qdrant_client
        if qdrant is None:
            logger.warning("Dream synthesis drain skipped — MemoryStore has no Qdrant client")
            return
    except Exception:
        logger.warning(
            "Pause check failed — skipping dream synthesis drain",
            exc_info=True,
        )
        return

    with contextlib.suppress(Exception):
        GenesisRuntime.instance().record_job_start("dream_synthesis_drain")

    try:
        from genesis.memory import dream_cycle

        rt._heavy_workload = "dream_synthesis_drain"
        rt._heavy_workload_since = datetime.now(UTC)

        # SHADOW hardwired: the live flip is a separate user-gated change.
        report = await dream_cycle.run_synthesis_drain(
            qdrant=qdrant,
            db=rt.db,
            router=rt.router,
            store=store,
            dry_run=True,
        )

        try:
            import uuid as _uuid  # noqa: PLC0415

            from genesis.db.crud import observations as obs_crud

            await obs_crud.create(
                rt.db,
                id=str(_uuid.uuid4()),
                source="dream_cycle",
                type="dream_synthesis_drain_report",
                content=(
                    f"Dream synthesis drain "
                    f"{'SHADOW' if report.get('dry_run') else 'LIVE'}: "
                    f"{report.get('drained', 0)} drained, "
                    f"{report.get('would_merge', 0)} would merge, "
                    f"{report.get('stale_skipped', 0)} stale, "
                    f"{len(report.get('errors', []))} errors"
                ),
                priority="low",
                created_at=datetime.now(UTC).isoformat(),
            )
        except Exception:
            pass

        logger.info("Dream synthesis drain complete: %s", report)
        with contextlib.suppress(Exception):
            GenesisRuntime.instance().record_job_success("dream_synthesis_drain")
    except Exception as exc:
        logger.exception("Dream synthesis drain failed: %s", exc)
        try:
            from genesis.runtime import GenesisRuntime

            GenesisRuntime.instance().record_job_failure(
                "dream_synthesis_drain",
                exc=exc,
            )
        except Exception as rec_err:
            logger.error(
                "Failed to record dream_synthesis_drain failure: %s (original error: %s)",
                rec_err,
                exc,
            )
    finally:
        # Clear only if this job set the flag (see run_dream_cycle note).
        try:
            if rt._heavy_workload == "dream_synthesis_drain":
                rt._heavy_workload = None
                rt._heavy_workload_since = None
        except Exception:
            pass
