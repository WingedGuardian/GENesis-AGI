"""Add ``calibration_cells`` + ``calibration_cell_history`` — WS-2 P3.

The unified calibration table (design §4): one row per
(domain, action_class, metric, provenance, window_days) cell, recomputed by
the mechanical grader at the end of each grading pass from resolved
``ledger_predictions`` rows plus per-tool base rates from
``tool_call_outcomes``. Stores the Murphy decomposition
(brier = reliability − resolution + uncertainty), ECE, and a Beta-binomial
shrunk estimate; ``status`` labels cold-start honesty (ok / thin / unknown —
thin and unknown cells must never be rendered as bare percentages by any
consumer).

``calibration_cell_history`` appends one snapshot per cell per recompute for
trend surfaces; retention is pruned in the same grading pass (180 days) so
the store stays bounded on every install.

Fresh/test DBs get the tables from the canonical CREATE TABLE in
``db/schema/_tables.py``; this numbered migration covers the existing-DB
upgrade path. Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. No commit — the
runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS calibration_cells (
            domain          TEXT NOT NULL,
            action_class    TEXT NOT NULL,
            metric          TEXT NOT NULL,
            provenance      TEXT NOT NULL
                              CHECK (provenance IN ('stated','policy_prior','all')),
            window_days     INTEGER NOT NULL,   -- 30, 90, 0 = all-time
            n               INTEGER NOT NULL,
            n_mechanical    INTEGER NOT NULL,
            base_rate       REAL,
            mean_confidence REAL,
            brier           REAL,
            reliability     REAL,               -- Murphy decomposition
            resolution      REAL,               -- the informativeness term
            uncertainty     REAL,
            ece             REAL,
            shrunk_estimate REAL,
            status          TEXT NOT NULL CHECK (status IN ('ok','thin','unknown')),
            computed_at     TEXT NOT NULL,
            PRIMARY KEY (domain, action_class, metric, provenance, window_days)
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS calibration_cell_history (
            id           TEXT PRIMARY KEY,
            domain       TEXT NOT NULL,
            action_class TEXT NOT NULL,
            metric       TEXT NOT NULL,
            provenance   TEXT NOT NULL,
            window_days  INTEGER NOT NULL,
            n            INTEGER NOT NULL,
            brier        REAL,
            reliability  REAL,
            resolution   REAL,
            ece          REAL,
            status       TEXT NOT NULL,
            snapshot_at  TEXT NOT NULL
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_cch_cell_time "
        "ON calibration_cell_history(domain, metric, snapshot_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS calibration_cell_history")
    await db.execute("DROP TABLE IF EXISTS calibration_cells")
