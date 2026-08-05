"""Scheduled memory-integrity jobs — Phase 0 detection + Phase 1 repair.

Restart-safe off-peak jobs, registered on the learning scheduler via a
testable seam (the ``_wire_drip_retention_jobs`` precedent):

- ``recall_health_probe`` (03:20 local) — the golden-set recall-health probe
  through the real retriever.
- ``memory_consistency_check`` (03:50 local) — the read-only cross-backend
  consistency scan (memory_metadata <-> Qdrant <-> memory_fts).
- ``memory_reconcile`` (04:40 local, ``mode=active`` only) — the Phase-1
  repair lane (``memory/integrity_repair.py``): deletes aged ghost points
  (payloads exported first) and re-queues aged lying mirrors for re-embed.
- ``memory_integrity_prune`` (04:15 local) — retention for the run tables.

All no-op outside their required mode (recording success so a deliberately
idle job looks healthy, not stuck), read all knobs live from the config, and
persist one row per run. The persisted rows are the fact store the awareness
posture check and the dashboard tile read — the jobs never raise alerts
themselves (decoupled surfacing).
"""

from __future__ import annotations

import logging

from genesis.env import user_timezone
from genesis.memory import integrity_config

logger = logging.getLogger(__name__)


def _wire_memory_integrity_jobs(scheduler, rt) -> None:
    """Register the two Phase-0 integrity jobs on *scheduler*."""
    from apscheduler.triggers.cron import CronTrigger

    async def _run_recall_probe() -> None:
        if rt._db is None:
            return
        if integrity_config.effective_mode() == "off":
            rt.record_job_success("recall_health_probe")
            return
        try:
            from genesis.db.crud import memory_integrity as mi_crud
            from genesis.memory.recall_probe import run_recall_probe

            cfg = integrity_config.load_config()
            result = await run_recall_probe(
                db=rt._db,
                retriever=rt._hybrid_retriever,
                probe_limit=integrity_config.knob_int(cfg, "probe_limit"),
                max_probes=integrity_config.knob_int(cfg, "max_probes_per_run"),
                rerank=bool(cfg.get("rerank", True)),
                rerank_timeout_s=integrity_config.knob_float(cfg, "rerank_timeout_s"),
                min_golden_for_status=integrity_config.knob_int(cfg, "min_golden_for_status"),
                baseline_window_runs=integrity_config.knob_int(cfg, "baseline_window_runs"),
                baseline_min_runs=integrity_config.knob_int(cfg, "baseline_min_runs"),
                drift_band=integrity_config.knob_float01(cfg, "drift_band"),
            )
            await mi_crud.insert_recall_probe_run(
                rt._db,
                status=result.status,
                probes_total=result.probes_total,
                probes_hit=result.probes_hit,
                hit_rate=result.hit_rate,
                mean_rr=result.mean_rr,
                baseline_hit_rate=result.baseline_hit_rate,
                drift=result.drift,
                details=result.details,
                unknown_reason=result.unknown_reason,
                duration_ms=result.duration_ms,
            )
            rt.record_job_success("recall_health_probe")
            logger.info(
                "recall health probe: status=%s hit_rate=%s drift=%s",
                result.status,
                result.hit_rate,
                result.drift,
            )
        except Exception as exc:
            rt.record_job_failure("recall_health_probe", exc=exc)
            logger.exception("recall health probe failed")

    scheduler.add_job(
        _run_recall_probe,
        CronTrigger(hour=3, minute=20, timezone=user_timezone()),
        id="recall_health_probe",
        max_instances=1,
        misfire_grace_time=3600,
    )

    async def _run_consistency_check() -> None:
        if rt._db is None:
            return
        if integrity_config.effective_mode() == "off":
            rt.record_job_success("memory_consistency_check")
            return
        try:
            from genesis.db.crud import memory_integrity as mi_crud
            from genesis.memory.integrity import run_consistency_check
            from genesis.qdrant.collections import get_client

            cfg = integrity_config.load_config()
            report = await run_consistency_check(
                qdrant_client=get_client(),
                sample_fraction=integrity_config.knob_float01(cfg, "sample_fraction"),
                max_points=integrity_config.knob_int(cfg, "max_points"),
                severe_min_count=integrity_config.knob_int(cfg, "severe_min_count"),
                pollution_min_count=integrity_config.knob_int(cfg, "pollution_min_count"),
                pollution_fraction=integrity_config.knob_float01(cfg, "pollution_fraction"),
                max_offender_sample=integrity_config.knob_int(cfg, "max_offender_sample"),
            )
            await mi_crud.insert_consistency_report(
                rt._db,
                status=report.status,
                counts=report.counts,
                total_rows=report.total_rows,
                sampled_rows=report.sampled_rows,
                sample_fraction=report.sample_fraction,
                truncated=report.truncated,
                offender_sample=report.offender_sample,
                unknown_reason=report.unknown_reason,
                duration_ms=report.duration_ms,
            )
            rt.record_job_success("memory_consistency_check")
            logger.info(
                "memory consistency check: status=%s findings=%d",
                report.status,
                report.total_findings,
            )
        except Exception as exc:
            rt.record_job_failure("memory_consistency_check", exc=exc)
            logger.exception("memory consistency check failed")

    scheduler.add_job(
        _run_consistency_check,
        CronTrigger(hour=3, minute=50, timezone=user_timezone()),
        id="memory_consistency_check",
        max_instances=1,
        misfire_grace_time=3600,
    )

    async def _run_memory_reconcile() -> None:
        if rt._db is None:
            return
        # Phase-1 repair runs ONLY under 'active'. 'passive' and 'off' are both
        # healthy no-ops here — the mode says don't repair, and a deliberately
        # idle job must not read as stuck.
        if integrity_config.effective_mode() != "active":
            rt.record_job_success("memory_reconcile")
            return
        try:
            from genesis.db.crud import memory_integrity as mi_crud
            from genesis.memory.integrity_repair import run_reconcile
            from genesis.qdrant.collections import get_client

            cfg = integrity_config.load_config()
            store = getattr(rt, "memory_store", None)
            result = await run_reconcile(
                db=rt._db,
                qdrant_client=get_client(),
                min_age_seconds=integrity_config.knob_int(cfg, "repair_min_age_seconds"),
                max_repairs_per_run=integrity_config.knob_int(cfg, "max_repairs_per_run"),
                max_points=integrity_config.knob_int(cfg, "max_points"),
                # Tombstone drain re-attempts deferred deletes through the full
                # store cascade; on a degraded boot (no store) the drain is
                # skipped loudly and the run reports partial.
                delete_memory=store.delete if store is not None else None,
            )
            await mi_crud.insert_reconcile_run(
                rt._db,
                status=result.status,
                ghosts_deleted=result.ghosts_deleted,
                ghost_delete_failed=result.ghost_delete_failed,
                mirrors_requeued=result.mirrors_requeued,
                mirrors_skipped_no_content=result.mirrors_skipped_no_content,
                tombstones_drained=result.tombstones_drained,
                truncated=result.truncated,
                capped=result.capped,
                duration_ms=result.duration_ms,
                details=result.details,
                unknown_reason=result.unknown_reason,
            )
            rt.record_job_success("memory_reconcile")
            logger.info(
                "memory reconcile: status=%s ghosts_deleted=%d mirrors_requeued=%d",
                result.status,
                result.ghosts_deleted,
                result.mirrors_requeued,
            )
        except Exception as exc:
            rt.record_job_failure("memory_reconcile", exc=exc)
            logger.exception("memory reconcile failed")
            # Best-effort audit row so a mid-run crash is visible in the run
            # history, not only in job-health.
            try:
                from genesis.db.crud import memory_integrity as mi_crud

                await mi_crud.insert_reconcile_run(
                    rt._db,
                    status="failed",
                    unknown_reason=f"exception: {type(exc).__name__}",
                )
            except Exception:
                logger.warning("memory reconcile: failed-run audit row not written", exc_info=True)

    scheduler.add_job(
        _run_memory_reconcile,
        CronTrigger(hour=4, minute=40, timezone=user_timezone()),
        id="memory_reconcile",
        max_instances=1,
        misfire_grace_time=3600,
    )

    async def _prune_memory_integrity() -> None:
        if rt._db is None:
            return
        try:
            from datetime import UTC, datetime

            from genesis.db.crud import memory_integrity as mi_crud

            cfg = integrity_config.load_config()
            now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            removed = await mi_crud.prune_memory_integrity(
                rt._db,
                older_than_days=integrity_config.knob_int(cfg, "retention_days"),
                now=now,
            )
            rt.record_job_success("memory_integrity_prune")
            if removed:
                logger.info("memory_integrity prune: removed %d rows", removed)
        except Exception as exc:
            rt.record_job_failure("memory_integrity_prune", exc=exc)
            logger.exception("memory_integrity prune failed")

    scheduler.add_job(
        _prune_memory_integrity,
        CronTrigger(hour=4, minute=15, timezone=user_timezone()),
        id="memory_integrity_prune",
        max_instances=1,
        misfire_grace_time=3600,
    )
