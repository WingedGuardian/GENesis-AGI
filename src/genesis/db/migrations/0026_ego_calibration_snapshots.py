"""Create ``ego_calibration_snapshots`` — measure-only ego calibration over time.

Each row is one snapshot of the ego's confidence calibration for a domain,
computed from Outcome Bus T1 (ground-truth) rows: does the ego's stated
confidence track actual success ("says 90%, right 82%")? One row per (domain,
run) so the ECE-over-time TREND accrues — the actual self-improvement signal.

DELIBERATELY a SEPARATE table from ``calibration_curves``: that shared table is
auto-read by ``perception/context.py:_build_calibration_text`` (it injects every
domain it finds into the perception context). Writing ego calibration there would
silently change ego/perception behaviour. This table has NO readers except the
read-only user surface — keeping the measurement DARK. Injecting calibration back
into the ego (self-correction) is a deliberate, separately-flagged future PR.

``sample_count`` is the count of calibratable T1 rows (both stated_confidence and
value present) that were actually bucketed — NOT a raw row count. ``low_confidence``
flags a statistically thin estimate so the surface never reports a noisy ECE=0.0 as
"perfectly calibrated".

Idempotent (``IF NOT EXISTS``). Fresh installs get the same DDL via ``_tables.py``.
"""

from __future__ import annotations

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ego_calibration_snapshots (
        id              TEXT PRIMARY KEY,
        domain          TEXT NOT NULL,
        ece             REAL NOT NULL,
        mce             REAL NOT NULL,
        sample_count    INTEGER NOT NULL,
        bucket_count    INTEGER NOT NULL,
        low_confidence  INTEGER NOT NULL DEFAULT 0,
        curve_json      TEXT NOT NULL,
        computed_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_ego_calibration_domain_time "
    "ON ego_calibration_snapshots(domain, computed_at)",
]


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (development/testing only)."""
    await db.execute("DROP TABLE IF EXISTS ego_calibration_snapshots")
