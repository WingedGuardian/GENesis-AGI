"""Add memory_consistency_reports + recall_probe_runs — Phase 0 memory integrity.

The "make silence loud" observability spine. Memory recall degrades silently:
the episodic write path fans out to three backends (Qdrant ``episodic_memory``,
``memory_metadata``, ``memory_fts``) with no cross-store transaction, and
nothing verifies they agree. These two tables persist the periodic read-only
checks so drift becomes a standing signal instead of an invisible quality loss.

- ``memory_consistency_reports``: one row per consistency-check run. Records the
  cross-backend classification counts (lying_mirror / ghost_points /
  fts_ghosts / fts_invisible / unexpected_vector / deprecated_divergence), the
  sample fraction used, and a capped offender-id sample. ``status`` is
  ``unknown`` when a dependency (Qdrant/FTS) was unavailable — a dependency
  failure must never masquerade as data corruption.

- ``recall_probe_runs``: one row per recall-health probe run. Records hit-rate
  and mean-reciprocal-rank of an install-local golden query->expected-memory
  set run through the REAL recall pipeline, plus drift vs a trailing baseline.
  ``status`` is ``unknown`` when the golden set is too small to judge.

Why two purpose-built tables rather than reusing ``eval_runs``: that table is
model-evaluation-semantic (``model_id`` / ``dataset`` / ``task_category`` are
NOT NULL and rows surface in ``genesis eval results/compare`` model-comparison
views). Store-integrity snapshots and recall-health probes are neither model
evals nor bench A/Bs; forcing them there would require lying field values and
pollute the model-eval views. The golden set itself is NOT a table — it is an
install-local file under ``~/.genesis/eval/golden/`` per the #1143 convention.

Additive + idempotent; DDL mirrored in ``db/schema/_tables.py``. Individual
``db.execute()`` calls, no commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_consistency_reports (
            id                   TEXT PRIMARY KEY,          -- uuid4 hex
            created_at           TEXT NOT NULL DEFAULT (datetime('now')),
            status               TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'unknown')),
            sample_fraction      REAL,                      -- 0..1 fraction of rows scanned
            sampled_rows         INTEGER,                   -- metadata rows actually checked
            total_rows           INTEGER,                   -- total memory_metadata rows
            truncated            INTEGER NOT NULL DEFAULT 0, -- 1 = a scan hit its budget cap
            counts_json          TEXT NOT NULL,             -- {class: count} for all classes
            offender_sample_json TEXT,                      -- {class: [ids...]} capped sample
            unknown_reason       TEXT,                      -- set iff status='unknown'
            duration_ms          INTEGER
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS recall_probe_runs (
            id                TEXT PRIMARY KEY,             -- uuid4 hex
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            status            TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'unknown')),
            probes_total      INTEGER,                      -- golden cases attempted
            probes_hit        INTEGER,                      -- cases where an expected id was retrieved
            hit_rate          REAL,                         -- probes_hit / probes_total
            mean_rr           REAL,                         -- mean reciprocal rank of first expected hit
            baseline_hit_rate REAL,                         -- trailing-window mean, NULL during observation
            drift             REAL,                         -- baseline_hit_rate - hit_rate, NULL if no baseline
            details_json      TEXT,                         -- per-case {id, hit, rank}
            unknown_reason    TEXT,                         -- set iff status='unknown'
            duration_ms       INTEGER
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcr_created ON memory_consistency_reports(created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcr_status ON memory_consistency_reports(status, created_at)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_rpr_created ON recall_probe_runs(created_at)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rpr_status ON recall_probe_runs(status, created_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS memory_consistency_reports")
    await db.execute("DROP TABLE IF EXISTS recall_probe_runs")
