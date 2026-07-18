"""Add ``autonomy_events`` — append-only success/correction evidence ledger.

The earn-back gate (``AutonomyManager.detect_earnback_candidates``) previously
computed the Bayesian posterior over LIFETIME counters
(``autonomy_state.total_successes`` / ``total_corrections``). After a deep
regression that gate is mathematically unreachable: a category with a long
mixed history needs dozens of consecutive successes before the lifetime
posterior recovers, so demoted autonomy stays pinned for months with no
path back — while the awareness signal reports the regression forever.

This ledger records each success/correction WITH its timestamp so eligibility
can be computed over a recent evidence window (``earnback.window_days`` in
``config/autonomy.yaml``). Regression math is untouched — lifetime counters
remain the input for ``record_correction``'s level drops.

Deliberately NO backfill: lifetime counters predate this ledger, and
fabricating ``occurred_at`` timestamps would manufacture evidence. Categories
become earn-back-eligible as genuine windowed evidence accumulates.

Fresh/test DBs get the table from the canonical CREATE TABLE in
``db/schema/_tables.py``; this numbered migration covers the existing-DB
upgrade path. Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. No commit — the
runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS autonomy_events (
            id          TEXT PRIMARY KEY,
            category    TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('success', 'correction')),
            occurred_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_events_cat_time "
        "ON autonomy_events(category, occurred_at)"
    )
