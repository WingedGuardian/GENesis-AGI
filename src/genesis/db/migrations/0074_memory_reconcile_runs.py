"""Add memory_reconcile_runs — Phase 1 memory-integrity repair lane.

Phase 0 (migration 0073) made cross-store memory drift observable; the d0008
data migration repaired what had already accumulated, once. The residual
lock-free dual-write windows between SQLite and Qdrant mean drift slowly
recurs, so Phase 1 adds a periodic reconcile job (the ``active`` mode that
``integrity_config`` reserved) — and this table persists one row per repair
run so the lane's actions are auditable and its health observable.

- ``memory_reconcile_runs``: one row per reconcile run. Records how many ghost
  points were deleted (payloads exported first), how many delete attempts
  failed (left for the next run), how many lying mirrors were re-queued for
  re-embedding, how many were marked 'failed' for having no recoverable FTS
  content, and how many deferred-delete tombstones were drained (0 until the
  tombstone PR lands — the column exists now to avoid an ALTER later).
  ``truncated`` records that the point scroll hit its budget (mirror repair is
  skipped under truncation — vector ABSENCE cannot be proven from a partial
  point set); ``capped`` records that the per-run repair cap cut the work list.
  ``status='skipped'`` with ``unknown_reason`` records a run that touched
  nothing because a dependency (Qdrant) was unavailable — a dependency outage
  must never look like "nothing needed repair".

Why not reuse ``memory_consistency_reports``: that table is detection-semantic
(one row per read-only scan, counts of what EXISTS) and is read by the
awareness posture check and the dashboard tile by that meaning. Repair runs
record what was CHANGED — mixing them would overload ``status`` and pollute
the posture read. Same separation argument as 0073's eval_runs rationale.

Additive + idempotent; DDL mirrored in ``db/schema/_tables.py``. Individual
``db.execute()`` calls, no commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_reconcile_runs (
            id                        TEXT PRIMARY KEY,          -- uuid4 hex
            created_at                TEXT NOT NULL DEFAULT (datetime('now')),
            status                    TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'skipped', 'failed')),
            ghosts_deleted            INTEGER NOT NULL DEFAULT 0,
            ghost_delete_failed       INTEGER NOT NULL DEFAULT 0,
            mirrors_requeued          INTEGER NOT NULL DEFAULT 0,
            mirrors_skipped_no_content INTEGER NOT NULL DEFAULT 0,
            tombstones_drained        INTEGER NOT NULL DEFAULT 0,
            truncated                 INTEGER NOT NULL DEFAULT 0, -- 1 = point scroll hit budget (mirrors skipped)
            capped                    INTEGER NOT NULL DEFAULT 0, -- 1 = per-run repair cap cut the work list
            duration_ms               INTEGER,
            details_json              TEXT,                       -- run detail (skipped-stale counts, samples)
            unknown_reason            TEXT                        -- set iff status='skipped'/'failed' on dependency outage
        )
        """
    )
    # Index names IDENTICAL to the db/schema/_tables.py mirror (0073's idx_mcr_*
    # convention) so IF NOT EXISTS dedups across the two build paths.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mrr_created ON memory_reconcile_runs(created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mrr_status ON memory_reconcile_runs(status, created_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP INDEX IF EXISTS idx_mrr_status")
    await db.execute("DROP INDEX IF EXISTS idx_mrr_created")
    await db.execute("DROP TABLE IF EXISTS memory_reconcile_runs")
