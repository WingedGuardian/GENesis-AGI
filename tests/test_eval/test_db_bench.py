"""Tests for eval.db.get_bench_comparisons — the bench A/B read query."""

from __future__ import annotations

import pytest

from genesis.eval.db import get_bench_comparisons

# eval_runs is migration-only (not in create_all_tables); mirror migration
# 0002_add_eval_tables, as the experimentation conftest does.
_EVAL_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY, model_id TEXT NOT NULL, model_profile TEXT,
    dataset TEXT NOT NULL, trigger TEXT NOT NULL, task_category TEXT NOT NULL,
    total_cases INTEGER NOT NULL, passed_cases INTEGER NOT NULL,
    failed_cases INTEGER NOT NULL, skipped_cases INTEGER DEFAULT 0,
    aggregate_score REAL, scores_json TEXT, metadata_json TEXT,
    comparison_run_id TEXT, created_at TEXT NOT NULL, duration_s REAL
)
"""


@pytest.fixture
async def eval_db(db):
    await db.execute(_EVAL_RUNS_DDL)
    await db.commit()
    return db


async def _insert(db, *, run_id, profile, created_at, meta="{}"):
    await db.execute(
        """INSERT INTO eval_runs
           (id, model_id, model_profile, dataset, trigger, task_category,
            total_cases, passed_cases, failed_cases, created_at, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, "sonnet", profile, "bench_v1", "manual", "agentic", 9, 7, 2, created_at, meta),
    )
    await db.commit()


async def test_filters_to_genesis_arm_only(eval_db):
    await _insert(
        eval_db,
        run_id="r1-genesis",
        profile="bench:genesis",
        created_at="2026-07-10T04:00:00+00:00",
    )
    await _insert(
        eval_db, run_id="r1-bare", profile="bench:bare", created_at="2026-07-10T04:00:00+00:00"
    )
    await _insert(
        eval_db, run_id="model-eval", profile="sonnet", created_at="2026-07-10T05:00:00+00:00"
    )

    rows = await get_bench_comparisons(eval_db)
    assert [r["id"] for r in rows] == ["r1-genesis"]
    assert rows[0]["model_profile"] == "bench:genesis"


async def test_newest_first(eval_db):
    await _insert(
        eval_db,
        run_id="older-genesis",
        profile="bench:genesis",
        created_at="2026-07-10T00:00:00+00:00",
    )
    await _insert(
        eval_db,
        run_id="newer-genesis",
        profile="bench:genesis",
        created_at="2026-07-11T00:00:00+00:00",
    )

    rows = await get_bench_comparisons(eval_db)
    assert [r["id"] for r in rows] == ["newer-genesis", "older-genesis"]


async def test_respects_limit(eval_db):
    for i in range(5):
        await _insert(
            eval_db,
            run_id=f"g{i}",
            profile="bench:genesis",
            created_at=f"2026-07-1{i}T00:00:00+00:00",
        )
    rows = await get_bench_comparisons(eval_db, limit=2)
    assert len(rows) == 2
    assert rows[0]["id"] == "g4"  # newest


async def test_empty_returns_empty_list(eval_db):
    rows = await get_bench_comparisons(eval_db)
    assert rows == []
