"""Persistence + observation tests for the skill-replay gate.

In-memory aiosqlite; assert exactly the paired eval_runs rows + one observation,
with priority keyed to the verdict. Fixed timestamps, no live clock.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.eval.bench.types import BenchArmOutcome, BenchTask
from genesis.eval.skill_replay.persist import (
    log_replay_observation,
    persist_skill_replay_summary,
)
from genesis.eval.skill_replay.types import (
    VERDICT_NET_POSITIVE,
    VERDICT_REGRESSION,
    SkillReplayPair,
    SkillReplayReport,
    SkillReplayVerdict,
)

_NOW = "2026-07-20T00:00:00+00:00"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    from importlib import import_module

    from genesis.db.schema import create_all_tables, seed_data

    await create_all_tables(conn)
    await seed_data(conn)
    # eval_runs / eval_results are migration-only (not in create_all_tables);
    # apply the eval-table migrations in order (0003 adds .skipped, 0014
    # adds .metadata_json — both written by insert_run).
    for mig in (
        "0002_add_eval_tables",
        "0003_eval_results_skipped",
        "0014_eval_results_metadata",
    ):
        await import_module(f"genesis.db.migrations.{mig}").up(conn)
    yield conn
    await conn.close()


def _outcome(arm: str, score: float, passed: bool) -> BenchArmOutcome:
    return BenchArmOutcome(
        task_id="t1",
        arm=arm,
        output_text=f"{arm}-output",
        judge_passed=passed,
        judge_score=score,
        judge_detail="{}",
    )


def _report(verdict_label: str, n_reg: int, n_imp: int) -> SkillReplayReport:
    task = BenchTask(id="t1", category="drafting", prompt="p", expected="e")
    pair = SkillReplayPair(
        task=task,
        old=_outcome("old", 0.5, False),
        new=_outcome("new", 0.9, True),
    )
    return SkillReplayReport(
        run_id="run1",
        skill_name="voice-master",
        model="sonnet",
        effort="medium",
        task_set_version="v1",
        task_file_sha256="abc123",
        rubric_name="bench_task_success",
        rubric_version="1.0",
        pairs=[pair],
        verdict=SkillReplayVerdict(
            verdict=verdict_label,
            n_complete=1,
            n_regressions=n_reg,
            n_improvements=n_imp,
            note="x",
        ),
    )


async def test_persist_writes_paired_eval_runs(db):
    report = _report(VERDICT_NET_POSITIVE, 0, 1)
    control_id, treatment_id = await persist_skill_replay_summary(db, report)

    cur = await db.execute(
        "SELECT id, model_profile, comparison_run_id FROM eval_runs ORDER BY model_profile"
    )
    rows = await cur.fetchall()
    assert len(rows) == 2
    profiles = {r["model_profile"] for r in rows}
    assert profiles == {"skill_replay:old", "skill_replay:new"}
    # Treatment (new) links back to control (old).
    treatment = next(r for r in rows if r["model_profile"] == "skill_replay:new")
    assert treatment["comparison_run_id"] == control_id
    assert report.control_run_id == control_id
    assert report.treatment_run_id == treatment_id
    # Two arms x one case = two eval_results.
    cur = await db.execute("SELECT COUNT(*) AS n FROM eval_results")
    assert (await cur.fetchone())["n"] == 2


async def test_observation_regression_is_high_priority(db):
    report = _report(VERDICT_REGRESSION, 1, 0)
    obs_id = await log_replay_observation(db, report, now=_NOW)

    cur = await db.execute(
        "SELECT source, type, priority, content FROM observations WHERE id = ?",
        (obs_id,),
    )
    row = await cur.fetchone()
    assert row["source"] == "skill_evolution_gate"
    assert row["type"] == "skill_replay_verdict"
    assert row["priority"] == "high"
    assert "regression" in row["content"]


async def test_observation_net_positive_is_low_priority(db):
    report = _report(VERDICT_NET_POSITIVE, 0, 1)
    await log_replay_observation(db, report, now=_NOW)

    cur = await db.execute("SELECT priority FROM observations WHERE type = 'skill_replay_verdict'")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["priority"] == "low"
