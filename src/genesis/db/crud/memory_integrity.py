"""CRUD for memory_consistency_reports + recall_probe_runs + memory_reconcile_runs.

Persistence for the "make silence loud" observability spine (migration 0073).
The consistency checker and recall-health probe write one row per run here; the
awareness posture check and the dashboard tile read the latest row per table;
``trailing_hit_rate`` supplies the recall-drift baseline.

Writers guard on table existence and no-op pre-migration (the repo_pulse
pattern) so a job scheduled before its migration lands never raises. Migrations
0073 (Phase-0 report/probe tables) and 0074 (Phase-1 reconcile-run table) are
the sole schema authorities; nothing here creates tables. Timestamps are
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
    # Tiebreak on rowid (monotonic insertion order), NOT id: ids are random
    # uuids, so two reports written in the same second would order
    # nondeterministically and "latest" could return the older row.
    cursor = await db.execute(
        "SELECT * FROM memory_consistency_reports ORDER BY created_at DESC, rowid DESC LIMIT 1"
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
    # rowid tiebreak (see latest_consistency_report) — deterministic "latest".
    cursor = await db.execute(
        "SELECT * FROM recall_probe_runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return _row_to_dict(cursor, row) if row else None


async def trailing_hit_rate(
    db: aiosqlite.Connection,
    *,
    window: int,
    exclude_current_id: str | None = None,
) -> tuple[float | None, int]:
    """Return ``(mean_hit_rate, n_runs)`` over the last *window* HEALTHY runs.

    Baseline = KNOWN-GOOD only: ``status = 'healthy'`` with a non-NULL
    ``hit_rate``. Deliberately NOT ``status != 'unknown'`` — including degraded
    runs would let a sustained hit-rate fall gradually normalize itself into the
    rolling baseline (drift → 0 → false 'healthy'). Because this selects the last
    N *healthy* runs (not the healthy runs among the last N), a run of degraded
    results never displaces the known-good baseline; the bar stays put until a
    genuinely healthy run replaces it. ``exclude_current_id`` omits the
    just-inserted row. ``(None, n)`` = observation period (no verdict yet).
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
        "WHERE status = 'healthy' AND hit_rate IS NOT NULL "  # noqa: S608 - static fragment
        f"{exclude_clause}"
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        params,
    )
    rows = await cursor.fetchall()
    rates = [float(r[0]) for r in rows]
    if not rates:
        return None, 0
    return sum(rates) / len(rates), len(rates)


async def earliest_probe_run_at(db: aiosqlite.Connection) -> str | None:
    """Oldest recall-probe ``created_at`` (or None). Staleness uses it to tell a
    fresh install from a genuinely dead/stuck probe (same as earliest_report_at)."""
    if not await _tables_available(db):
        return None
    cursor = await db.execute("SELECT MIN(created_at) FROM recall_probe_runs")
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def has_recent_conclusive_probe(db: aiosqlite.Connection, *, since_iso: str) -> bool:
    """True if a probe run with status in (healthy, degraded) exists at/after
    *since_iso*. A probe frozen on 'unknown' (retriever error) is inconclusive;
    'golden_set_too_small' is needs-setup, not a failure — both are excluded so
    the staleness check can distinguish a wedged probe from an unseeded one.
    Pre-migration -> True (nothing to alert on yet)."""
    if not await _tables_available(db):
        return True
    cursor = await db.execute(
        "SELECT 1 FROM recall_probe_runs "
        "WHERE status IN ('healthy', 'degraded') AND created_at >= ? "
        "LIMIT 1",
        (since_iso,),
    )
    return await cursor.fetchone() is not None


async def fetch_consistency_metadata(db: aiosqlite.Connection) -> list:
    """Return all ``memory_metadata`` rows the consistency checker needs
    (memory_id, collection, embedding_status, deprecated). Centralizes the
    full-corpus read so the checker doesn't inline SQL (crud convention); the
    checker passes its own read-only ``mode=ro`` connection."""
    return await db.execute_fetchall(
        "SELECT memory_id, collection, embedding_status, deprecated FROM memory_metadata"
    )


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


async def earliest_report_at(db: aiosqlite.Connection) -> str | None:
    """Return the oldest consistency-report ``created_at``, or None if there are
    no reports. Used by the staleness posture to distinguish a fresh install
    (checker younger than the stale window → silent) from a genuinely
    dead/stuck checker (running longer than the window with no good report)."""
    if not await _tables_available(db):
        return None
    cursor = await db.execute("SELECT MIN(created_at) FROM memory_consistency_reports")
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def prune_memory_integrity(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 90,
    now: str,
) -> int:
    """Delete report/probe/reconcile rows older than *older_than_days* vs ISO ``now``.

    Retention for the unbounded integrity stores (wired into the
    _wire_drip_retention_jobs family). ``now`` is injected (never wall-clock
    here) so the cutover is deterministic and testable. No-ops before migration
    0073; never creates tables. Returns total rows deleted.

    ``memory_reconcile_runs`` (migration 0074) prunes under its OWN guard —
    the Phase-0 tables must keep pruning on an install where 0074 hasn't
    applied yet, and vice versa.
    """
    cutoff = f"datetime(?, '-{int(older_than_days)} days')"
    deleted = 0
    if await _tables_available(db):
        for table in ("memory_consistency_reports", "recall_probe_runs"):
            cursor = await db.execute(
                f"DELETE FROM {table} WHERE created_at < {cutoff}",  # noqa: S608 - table names are literals; now bound
                (now,),
            )
            deleted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    if await _reconcile_table_available(db):
        cursor = await db.execute(
            f"DELETE FROM memory_reconcile_runs WHERE created_at < {cutoff}",  # noqa: S608 - now bound
            (now,),
        )
        deleted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    await db.commit()
    return deleted


# ── memory_reconcile_runs (Phase 1 repair lane, migration 0074) ──────────

# Separate guard from _tables_available on purpose: extending that COUNT(*)==2
# check to 3 would silently no-op the Phase-0 report writers on any install
# where 0074 hasn't applied yet. Same caching contract: only TRUE is cached.
_reconcile_table_verified = False


async def _reconcile_table_available(db: aiosqlite.Connection) -> bool:
    global _reconcile_table_verified
    if _reconcile_table_verified:
        return True
    cursor = await db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = 'memory_reconcile_runs'"
    )
    row = await cursor.fetchone()
    exists = bool(row and row[0] == 1)
    if exists:
        _reconcile_table_verified = True
    return exists


async def insert_reconcile_run(
    db: aiosqlite.Connection,
    *,
    status: str,
    ghosts_deleted: int = 0,
    ghost_delete_failed: int = 0,
    mirrors_requeued: int = 0,
    mirrors_skipped_no_content: int = 0,
    tombstones_drained: int = 0,
    truncated: bool = False,
    capped: bool = False,
    duration_ms: int | None = None,
    details: dict | None = None,
    unknown_reason: str | None = None,
    created_at: str | None = None,
) -> str | None:
    """Insert one reconcile-run row. Returns the row id, or None pre-migration."""
    if not await _reconcile_table_available(db):
        return None
    run_id = uuid.uuid4().hex
    cols = [
        "id",
        "status",
        "ghosts_deleted",
        "ghost_delete_failed",
        "mirrors_requeued",
        "mirrors_skipped_no_content",
        "tombstones_drained",
        "truncated",
        "capped",
        "duration_ms",
        "details_json",
        "unknown_reason",
    ]
    vals = [
        run_id,
        status,
        int(ghosts_deleted),
        int(ghost_delete_failed),
        int(mirrors_requeued),
        int(mirrors_skipped_no_content),
        int(tombstones_drained),
        1 if truncated else 0,
        1 if capped else 0,
        duration_ms,
        json.dumps(details or {}, sort_keys=True),
        unknown_reason,
    ]
    if created_at is not None:
        cols.append("created_at")
        vals.append(created_at)
    placeholders = ", ".join("?" for _ in cols)
    await db.execute(
        f"INSERT INTO memory_reconcile_runs ({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608 - column names are literals above, values bound
        vals,
    )
    await db.commit()
    return run_id


async def latest_reconcile_run(db: aiosqlite.Connection) -> dict | None:
    """Return the most recent reconcile-run row as a dict, or None."""
    if not await _reconcile_table_available(db):
        return None
    # rowid tiebreak — same rationale as latest_consistency_report.
    cursor = await db.execute(
        "SELECT * FROM memory_reconcile_runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return _row_to_dict(cursor, row) if row else None
