"""Add metadata_json column to eval_results.

The LLM-as-judge scorer (genesis.eval.scorers.LLMJudgeScorer) packs
rubric_name, rubric_version, judge_model, judge_score, rationale, and
the raw judge response into a JSON string for the existing
``scorer_detail`` text column. Calibration tooling and downstream
analyses (e.g. per-rubric drift) need to query that data structurally
without re-parsing every row.

This migration adds a dedicated ``metadata_json`` TEXT column that the
runner / calibration job populates with the same JSON payload alongside
``scorer_detail``. The dual write is intentional: ``scorer_detail``
preserves the existing 3-tuple Scorer contract (string), and
``metadata_json`` is the queryable view.

Idempotent — ALTER TABLE ADD COLUMN with the same name is rejected by
SQLite, so we check sqlite_master first and skip if already applied.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(eval_results)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "metadata_json" not in cols:
        await db.execute(
            "ALTER TABLE eval_results ADD COLUMN metadata_json TEXT"
        )


async def down(db: aiosqlite.Connection) -> None:
    # SQLite supports ALTER TABLE DROP COLUMN since 3.35 (2021).
    cursor = await db.execute("PRAGMA table_info(eval_results)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "metadata_json" in cols:
        await db.execute(
            "ALTER TABLE eval_results DROP COLUMN metadata_json"
        )
