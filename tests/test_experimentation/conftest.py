"""Shared fixtures for experimentation tests."""

import pytest

# eval_runs is migration-only (not in create_all_tables). The experimentation
# harness only writes run-level rows (results=[]), so eval_results isn't needed.
# DDL mirrors migration 0002_add_eval_tables.
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
