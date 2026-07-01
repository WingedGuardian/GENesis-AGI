"""Add judge_score + judge_detail columns to surplus_tasks (quality judge).

The ``outcome_quality`` verdict on insight-producing surplus tasks is now set by
a measurement-only LLM quality judge (``surplus.quality_judge``) rather than the
old intake-routing heuristic (which was structurally unreachable — curated
surplus sources skip scoring and route at a fixed 0.6 confidence, so intake
never discarded everything, so 'hollow' could never fire). ``outcome_quality``
keeps its meaning at the *bus* level — ``useful`` / ``hollow`` / NULL — but now
means "judge passed / judge failed / no verdict" instead of
"intake kept something / intake discarded everything / n-a".

This migration adds the two continuous, human-readable columns behind that
binary verdict, for calibration and display (they are NOT read by the Outcome
Bus harvester, which only branches on ``outcome_quality``):

  - ``judge_score``  REAL : the judge's [0, 1] quality score.
  - ``judge_detail`` TEXT : the judge's JSON detail (rubric, model, rationale).

Additive and backward-compatible: existing rows backfill to NULL. Fresh DBs get
both columns from the canonical CREATE TABLE in db/schema/_tables.py.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Table may not exist if migrations run on a fresh DB before schema init.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='surplus_tasks'"
    )
    if not await cursor.fetchone():
        return

    # Only add columns that don't already exist (fresh DBs get them from the
    # canonical CREATE TABLE; re-runs must be idempotent).
    col_cursor = await db.execute("PRAGMA table_info(surplus_tasks)")
    cols = {row[1] for row in await col_cursor.fetchall()}

    if "judge_score" not in cols:
        await db.execute("ALTER TABLE surplus_tasks ADD COLUMN judge_score REAL")
    if "judge_detail" not in cols:
        await db.execute("ALTER TABLE surplus_tasks ADD COLUMN judge_detail TEXT")
