"""Tests for experiment persistence — two linked eval_runs rows, no new table."""

import json

import pytest

from genesis.eval.db import get_experiment_runs
from genesis.experimentation.persistence import persist_experiment
from genesis.experimentation.types import ArmResult, ExperimentResult

# eval_runs is migration-only (not in create_all_tables), and we only write
# run-level rows (results=[]) so eval_results isn't needed. Add eval_runs to the
# conftest in-memory db (DDL mirrors migration 0002_add_eval_tables).
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


def _result():
    winrate = {
        "recommendation": "control_wins",
        "significant": True,
        "n_control_wins": 6,
        "n_treatment_wins": 0,
    }
    return ExperimentResult(
        experiment_name="unit_test",
        control=ArmResult(
            variant_name="ctrl", case_scores=[0.8] * 6, case_results=[True] * 6,
            n_pass=6, mean_score=0.8,
        ),
        treatment=ArmResult(
            variant_name="trt", case_scores=[0.1] * 6, case_results=[False] * 6,
            n_pass=0, mean_score=0.1,
        ),
        winrate=winrate,
        n_cases=6,
        errors=0,
        metadata={"rubric_name": "reflection_quality", "pass_winrate": {"recommendation": "control_wins"}},
    )


async def test_persist_writes_two_linked_rows(eval_db):
    ids = await persist_experiment(
        eval_db, _result(), gen_provider="groq-free", judge_provider="nvidia-nim-deepseek",
    )
    assert set(ids) == {"control_run_id", "treatment_run_id"}

    runs = await get_experiment_runs(eval_db, limit=10)
    assert len(runs) == 2
    by_id = {r["id"]: r for r in runs}
    control = by_id[ids["control_run_id"]]
    treatment = by_id[ids["treatment_run_id"]]

    # control unlinked; treatment links back to control (A/B pairing)
    assert control["comparison_run_id"] is None
    assert treatment["comparison_run_id"] == ids["control_run_id"]
    assert control["trigger"] == "experiment"

    # aggregate scores landed on each arm
    assert control["aggregate_score"] == pytest.approx(0.8)
    assert treatment["aggregate_score"] == pytest.approx(0.1)

    # the recommendation is queryable from the treatment row's metadata
    meta = json.loads(treatment["metadata_json"])
    assert meta["arm"] == "treatment"
    assert meta["recommendation"] == "control_wins"
    assert meta["winrate"]["n_control_wins"] == 6
    assert meta["control_run_id"] == ids["control_run_id"]
