"""CRUD for memory_consistency_reports + recall_probe_runs — Phase 0 integrity.

Persistence for the "make silence loud" observability spine (migration 0073).
The consistency checker and recall-health probe write one row per run here; the
awareness posture check and the dashboard tile read the latest row per table;
``trailing_hit_rate`` supplies the recall-drift baseline.

Writers guard on table existence and no-op pre-migration (the repo_pulse
pattern) so a job scheduled before its migration lands never raises. Migration
0073 is the sole schema authority; nothing here creates tables. Timestamps are
injected (``now``) for deterministic, testable retention.
"""

from __future__ import annotations

import json
import uuid

import aiosqlite

# Per-process cache: only the TRUE result is cached — a missing table
# (pre-migration window) is re-checked every call so a job self-heals the
# moment the server migration lands.
_tables_verified = False


async def _tables_available(db: aiosqlite.Connection) -> bool:
    global _tables_verified
    if _tables_verified:
        return True
    cursor = await db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('memory_consistency_reports', 'recall_probe_runs')"
    )
    row = await cursor.fetchone()
    exists = bool(row and row[0] == 2)
    if exists:
        _tables_verified = True
    return exists


def _row_to_dict(cursor, row) -> dict:
    """Map a fetched row to a dict by cursor.description (connection-agnostic)."""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


# ── memory_consistency_reports ───────────────────────────────────────────


async def insert_consistency_report(
    db: aiosqlite.Connection,
    *,
    status: str,
    counts: dict[str, int],
    total_rows: int,
    sampled_rows: int,
    sample_fraction: float,
    truncated: bool,
    offender_sample: dict[str, list[str]] | None = None,
    unknown_reason: str | None = None,
    duration_ms: int | None = None,
    created_at: str | None = None,
) -> str | None:
    """Insert one consistency-check row. Returns the row id, or None pre-migration."""
    if not await _tables_available(db):
        return None
    report_id = uuid.uuid4().hex
    cols = [
        "id",
        "status",
        "sample_fraction",
        "sampled_rows",
        "total_rows",
        "truncated",
        "counts_json",
        "offender_sample_json",
        "unknown_reason",
        "duration_ms",
    ]
    vals = [
        report_id,
        status,
        sample_fraction,
        sampled_rows,
        total_rows,
        1 if truncated else 0,
        json.dumps(counts, sort_keys=True),
        json.dumps(offender_sample or {}, sort_keys=True),
        unknown_reason,
        duration_ms,
    ]
    if created_at is not None:
        cols.append("created_at")
        vals.append(created_at)
    placeholders = ", ".join("?" for _ in cols)
    await db.execute(
        f"INSERT INTO memory_consistency_reports ({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608 - column names are literals above, values bound
        vals,
    )
    await db.commit()
    return report_id


async def latest_consistency_report(db: aiosqlite.Connection) -> dict | None:
    """Return the most recent consistency report row as a dict, or None."""
    if not await _tables_available(db):
        return None
    cursor = await db.execute(
        "SELECT * FROM memory_consistency_reports ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return _row_to_dict(cursor, row) if row else None


# ── recall_probe_runs ────────────────────────────────────────────────────


async def insert_recall_probe_run(
    db: aiosqlite.Connection,
    *,
    status: str,
    probes_total: int,
    probes_hit: int,
    hit_rate: float | None,
    mean_rr: float | None,
    baseline_hit_rate: float | None = None,
    drift: float | None = None,
    details: list[dict] | None = None,
    unknown_reason: str | None = None,
    duration_ms: int | None = None,
    created_at: str | None = None,
) -> str | None:
    """Insert one recall-probe row. Returns the row id, or None pre-migration."""
    if not await _tables_available(db):
        return None
    run_id = uuid.uuid4().hex
    cols = [
        "id",
        "status",
        "probes_total",
        "probes_hit",
        "hit_rate",
        "mean_rr",
        "baseline_hit_rate",
        "drift",
        "details_json",
        "unknown_reason",
        "duration_ms",
    ]
    vals = [
        run_id,
        status,
        probes_total,
        probes_hit,
        hit_rate,
        mean_rr,
        baseline_hit_rate,
        drift,
        json.dumps(details or []),
        unknown_reason,
        duration_ms,
    ]
    if created_at is not None:
        cols.append("created_at")
        vals.append(created_at)
    placeholders = ", ".join("?" for _ in cols)
    await db.execute(
        f"INSERT INTO recall_probe_runs ({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608 - column names are literals above, values bound
        vals,
    )
    await db.commit()
    return run_id


async def latest_recall_probe_run(db: aiosqlite.Connection) -> dict | None:
    """Return the most recent recall-probe row as a dict, or None."""
    if not await _tables_available(db):
        return None
    cursor = await db.execute(
        "SELECT * FROM recall_probe_runs ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return _row_to_dict(cursor, row) if row else None


async def trailing_hit_rate(
    db: aiosqlite.Connection,
    *,
    window: int,
    exclude_current_id: str | None = None,
) -> tuple[float | None, int]:
    """Return ``(mean_hit_rate, n_runs)`` over the last *window* non-unknown runs.

    Only ``status != 'unknown'`` rows with a non-NULL ``hit_rate`` count toward
    the baseline (an unknown run measured nothing). ``exclude_current_id`` omits
    the just-inserted row so a run never baselines against itself. Returns
    ``(None, n)`` when no qualifying runs exist — the caller treats that as the
    observation period (no drift verdict yet).
    """
    if not await _tables_available(db):
        return None, 0
    params: list = []
    exclude_clause = ""
    if exclude_current_id is not None:
        exclude_clause = "AND id != ? "
        params.append(exclude_current_id)
    params.append(window)
    cursor = await db.execute(
        "SELECT hit_rate FROM recall_probe_runs "
        "WHERE status != 'unknown' AND hit_rate IS NOT NULL "  # noqa: S608 - static fragment
        f"{exclude_clause}"
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    )
    rows = await cursor.fetchall()
    rates = [float(r[0]) for r in rows]
    if not rates:
        return None, 0
    return sum(rates) / len(rates), len(rates)


async def has_recent_non_unknown_report(
    db: aiosqlite.Connection,
    *,
    since_iso: str,
) -> bool:
    """True if a consistency report with status in (healthy, degraded) exists at
    or after *since_iso*. Drives the staleness posture: a checker that has
    produced only ``unknown`` (or nothing) within the window is itself a silent
    failure and must escalate. Pre-migration → True (nothing to alert on yet).
    """
    if not await _tables_available(db):
        return True
    cursor = await db.execute(
        "SELECT 1 FROM memory_consistency_reports "
        "WHERE status IN ('healthy', 'degraded') AND created_at >= ? "
        "LIMIT 1",
        (since_iso,),
    )
    return await cursor.fetchone() is not None


async def prune_memory_integrity(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 90,
    now: str,
) -> int:
    """Delete report/probe rows older than *older_than_days* relative to ISO ``now``.

    Retention for the two unbounded integrity stores (wired into the
    _wire_drip_retention_jobs family). ``now`` is injected (never wall-clock
    here) so the cutover is deterministic and testable. No-ops before migration
    0073; never creates tables. Returns total rows deleted.
    """
    if not await _tables_available(db):
        return 0
    cutoff = f"datetime(?, '-{int(older_than_days)} days')"
    deleted = 0
    for table in ("memory_consistency_reports", "recall_probe_runs"):
        cursor = await db.execute(
            f"DELETE FROM {table} WHERE created_at < {cutoff}",  # noqa: S608 - table names are literals; now bound
            (now,),
        )
        deleted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    await db.commit()
    return deleted
