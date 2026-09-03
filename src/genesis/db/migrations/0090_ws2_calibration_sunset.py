"""WS-2 PR-5 sunset — retire the legacy calibration-curve / prediction loop.

The unified ``calibration_cells`` table (migration 0069, WS-2 P3) replaced the
legacy per-domain ``calibration_curves`` recompute and the proto-ledger
``predictions`` table. Both are now write-only / dead-read: perception reads
``calibration_cells`` (``perception/context.py``), nothing consumes
``calibration_curves``, and — with the ``PredictionLogger``/``PredictionReconciler``/
``CalibrationCurveComputer`` wiring removed in the same PR — nothing writes
``predictions`` either. This migration retires the two tables.

- ``calibration_curves`` → **dropped** (dead-read; no FK references it).
- ``predictions`` → **archive-renamed** to ``predictions_legacy_ws2`` (locked
  decision: no hard drop — preserve the legacy rows for reversibility).

**Build-path interaction (why the predictions branch is guarded).**
``create_all_tables`` (``db/schema/_migrations.py``) runs in the order
(1) create ``TABLES`` → (2) run this numbered runner → (3) create ``INDEXES``.
This PR removes both ``predictions`` and ``calibration_curves`` from ``TABLES``
(and the ``idx_predictions_*`` entries from ``INDEXES``), so:

- **Fresh install:** step 1 no longer creates either table; step 2 finds no
  ``predictions`` (skip the rename) and drops a non-existent ``calibration_curves``
  (``IF EXISTS`` no-op). Result: neither table exists — nothing to archive, because
  a fresh install never had legacy data.
- **Existing install:** both tables pre-exist from before this deploy; step 1
  does not recreate them (removed from ``TABLES``, so no resurrection on later
  boots either); step 2 drops ``calibration_curves`` and renames ``predictions``
  into the archive. The old ``idx_predictions_*`` indexes travel with the rename
  (SQLite retains them, keyed to the new table name) and are inert on the archive.

Idempotent: the rename fires only when ``predictions`` exists and the archive does
not, so a re-run (or the ``--apply`` path) is a no-op. The DROP is ``IF EXISTS``.
"""

from __future__ import annotations

import aiosqlite

# The pre-sunset calibration_curves DDL (kept here only so down() can restore it
# for development/testing — it is no longer in db/schema/_tables.py TABLES).
_CALIBRATION_CURVES_DDL = """
    CREATE TABLE IF NOT EXISTS calibration_curves (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        domain               TEXT NOT NULL,
        confidence_bucket    TEXT NOT NULL,
        predicted_confidence REAL NOT NULL,
        actual_success_rate  REAL NOT NULL,
        sample_count         INTEGER NOT NULL,
        correction_factor    REAL NOT NULL,
        computed_at          TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(domain, confidence_bucket)
    )
"""


async def _has_table(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return await cursor.fetchone() is not None


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.

    # calibration_curves: dead-read legacy table. Nothing reads it (perception
    # repointed to calibration_cells), no FK references it. Drop it.
    await db.execute("DROP TABLE IF EXISTS calibration_curves")

    # predictions: archive-rename (preserve legacy rows). Guarded so the branch
    # fires exactly once, on an existing install that still has the legacy table
    # and has not already been archived — see the module docstring for why fresh
    # installs and the isolation harness land in the skip path.
    if await _has_table(db, "predictions") and not await _has_table(db, "predictions_legacy_ws2"):
        await db.execute("ALTER TABLE predictions RENAME TO predictions_legacy_ws2")


async def down(db: aiosqlite.Connection) -> None:
    """Reverse the sunset (development/testing only).

    Restores ``calibration_curves`` (empty) and renames the archive back to
    ``predictions``. Guarded for idempotency and to avoid clobbering a live
    ``predictions`` table.
    """
    await db.execute(_CALIBRATION_CURVES_DDL)
    if await _has_table(db, "predictions_legacy_ws2") and not await _has_table(db, "predictions"):
        await db.execute("ALTER TABLE predictions_legacy_ws2 RENAME TO predictions")
