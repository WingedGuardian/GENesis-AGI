"""Add eval_runs and eval_results tables for the model evaluation harness.

Stores automated eval run summaries and per-case scoring results.
Used by the eval harness (src/genesis/eval/) and MODEL_EVAL surplus tasks.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS eval_runs (
            id TEXT PRIMARY KEY,
            model_id TEXT NOT NULL,
            model_profile TEXT,
            dataset TEXT NOT NULL,
            trigger TEXT NOT NULL,
            task_category TEXT NOT NULL,
            total_cases INTEGER NOT NULL,
            passed_cases INTEGER NOT NULL,
            failed_cases INTEGER NOT NULL,
            skipped_cases INTEGER DEFAULT 0,
            aggregate_score REAL,
            scores_json TEXT,
            metadata_json TEXT,
            comparison_run_id TEXT,
            created_at TEXT NOT NULL,
            duration_s REAL
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_eval_runs_model
        ON eval_runs (model_id, created_at)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_eval_runs_dataset
        ON eval_runs (dataset, created_at)
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES eval_runs(id),
            case_id TEXT NOT NULL,
            input_text TEXT NOT NULL,
            expected_output TEXT,
            actual_output TEXT,
            score REAL,
            passed INTEGER NOT NULL,
            scorer_type TEXT NOT NULL,
            scorer_detail TEXT,
            latency_ms REAL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            created_at TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_eval_results_run
        ON eval_results (run_id)
    """)


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS eval_results")
    await db.execute("DROP TABLE IF EXISTS eval_runs")
