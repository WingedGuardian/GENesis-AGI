"""Eval harness DB operations — insert and query eval_runs / eval_results."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

from genesis.eval.types import EvalRunSummary


async def insert_run(
    db: aiosqlite.Connection,
    summary: EvalRunSummary,
) -> str:
    """Insert an eval run and all its results. Returns run_id."""
    run_id = summary.run_id or uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()

    await db.execute(
        """INSERT INTO eval_runs
           (id, model_id, model_profile, dataset, trigger, task_category,
            total_cases, passed_cases, failed_cases, skipped_cases,
            aggregate_score, scores_json, metadata_json, created_at, duration_s)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            summary.model_id,
            summary.model_profile,
            summary.dataset,
            summary.trigger,
            summary.task_category,
            summary.total_cases,
            summary.passed_cases,
            summary.failed_cases,
            summary.skipped_cases,
            summary.aggregate_score,
            json.dumps(summary.scores) if summary.scores else None,
            json.dumps(summary.metadata) if summary.metadata else None,
            now,
            summary.duration_s,
        ),
    )

    for result in summary.results:
        await db.execute(
            """INSERT INTO eval_results
               (id, run_id, case_id, input_text, expected_output, actual_output,
                score, passed, skipped, scorer_type, scorer_detail, latency_ms,
                input_tokens, output_tokens, cost_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex,
                run_id,
                result.case_id,
                "",  # input_text stored in dataset, not duplicated
                "",  # expected_output stored in dataset
                result.actual_output,
                result.score,
                1 if result.passed else 0,
                1 if result.skipped else 0,
                result.scorer_type,
                result.scorer_detail,
                result.latency_ms,
                result.input_tokens,
                result.output_tokens,
                result.cost_usd,
                now,
            ),
        )

    await db.commit()
    return run_id


async def get_runs(
    db: aiosqlite.Connection,
    *,
    model_id: str | None = None,
    dataset: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Query eval runs with optional filters."""
    clauses: list[str] = []
    params: list = []
    if model_id:
        clauses.append("model_id = ?")
        params.append(model_id)
    if dataset:
        clauses.append("dataset = ?")
        params.append(dataset)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    cursor = await db.execute(
        f"SELECT * FROM eval_runs {where} ORDER BY created_at DESC LIMIT ?",
        params,
    )
    cols = [d[0] for d in cursor.description]
    rows = await cursor.fetchall()
    return [dict(zip(cols, row, strict=False)) for row in rows]


async def get_run_results(
    db: aiosqlite.Connection,
    run_id: str,
) -> list[dict]:
    """Get all results for a specific eval run."""
    cursor = await db.execute(
        "SELECT * FROM eval_results WHERE run_id = ? ORDER BY case_id",
        (run_id,),
    )
    cols = [d[0] for d in cursor.description]
    rows = await cursor.fetchall()
    return [dict(zip(cols, row, strict=False)) for row in rows]


async def get_latest_run(
    db: aiosqlite.Connection,
    model_id: str,
    dataset: str,
) -> dict | None:
    """Get the most recent eval run for a model+dataset pair."""
    cursor = await db.execute(
        """SELECT * FROM eval_runs
           WHERE model_id = ? AND dataset = ?
           ORDER BY created_at DESC LIMIT 1""",
        (model_id, dataset),
    )
    cols = [d[0] for d in cursor.description]
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(zip(cols, row, strict=False))
