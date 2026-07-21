"""Persistence + observation logging for a skill-replay run.

Two additive, reversible sinks:

  * :func:`persist_skill_replay_summary` — two paired ``eval_runs`` rows (OLD
    control, NEW treatment) via ``EvalTrigger.EXPERIMENT`` +
    ``metadata.kind='skill_replay_verdict'`` (NO migration — reuses the
    experiment trigger, like ``persist_evo_summary``), linked with
    ``set_comparison_run``.
  * :func:`log_replay_observation` — one observation
    (``source='skill_evolution_gate'``, ``type='skill_replay_verdict'``)
    carrying the recommend-only verdict, sibling to the static
    ``skill_edit_critic`` verdict. ``priority='high'`` for a regression so it
    surfaces during the shadow bake, ``'low'`` otherwise.

Depends only on the pure ``types`` module (no CC), so it stays DB-only.
"""

from __future__ import annotations

import json
import uuid
from statistics import fmean

from genesis.eval.skill_replay.types import (
    ARM_NEW,
    ARM_OLD,
    VERDICT_INCONCLUSIVE,
    VERDICT_REGRESSION,
    SkillReplayReport,
)
from genesis.eval.types import (
    EvalRunSummary,
    EvalTrigger,
    ScoredOutput,
    ScorerType,
    TaskCategory,
)

_DATASET_PREFIX = "skill_replay"
# eval_results.actual_output cap — bounded against a runaway transcript output.
_OUTPUT_PERSIST_CAP = 20_000


def _verdict_metadata(report: SkillReplayReport) -> dict:
    v = report.verdict
    return {
        "kind": "skill_replay_verdict",
        "skill_name": report.skill_name,
        "verdict": v.verdict if v else None,
        "n_complete": v.n_complete if v else 0,
        "n_regressions": v.n_regressions if v else 0,
        "n_improvements": v.n_improvements if v else 0,
        "score_winrate": v.score_winrate if v else {},
        "pass_winrate": v.pass_winrate if v else {},
        "note": v.note if v else "",
        "task_set_version": report.task_set_version,
        "task_file_sha256": report.task_file_sha256,
        "rubric": report.rubric_name,
        "rubric_version": report.rubric_version,
        "effort": report.effort,
        "notes": report.notes,
        "prod_delta_clean": report.prod_delta.get("clean"),
    }


def _arm_summary(
    report: SkillReplayReport,
    arm: str,
    run_id: str,
    duration_s: float,
    extra_metadata: dict,
) -> EvalRunSummary:
    outcomes = [(p.old if arm == ARM_OLD else p.new) for p in report.pairs]
    results = [
        ScoredOutput(
            case_id=o.task_id,
            passed=o.judge_passed,
            score=o.judge_score,
            actual_output=(o.output_text or o.skip_reason)[:_OUTPUT_PERSIST_CAP],
            scorer_type=ScorerType.LLM_JUDGE,
            scorer_detail=o.judge_detail or o.skip_reason,
            latency_ms=o.duration_s * 1000.0,
            input_tokens=o.input_tokens,
            output_tokens=o.output_tokens,
            cost_usd=o.cost_usd,
            skipped=o.skipped,
        )
        for o in outcomes
    ]
    live = [o for o in outcomes if not o.skipped]
    scores = [o.judge_score for o in live]
    return EvalRunSummary(
        run_id=run_id,
        model_id=report.model,
        model_profile=f"{_DATASET_PREFIX}:{arm}",
        dataset=f"{_DATASET_PREFIX}:{report.skill_name}",
        trigger=EvalTrigger.EXPERIMENT,
        task_category=TaskCategory.AGENTIC,
        total_cases=len(outcomes),
        passed_cases=sum(1 for o in live if o.judge_passed),
        failed_cases=sum(1 for o in live if not o.judge_passed),
        skipped_cases=sum(1 for o in outcomes if o.skipped),
        aggregate_score=fmean(scores) if scores else 0.0,
        scores={"mean_judge_score": fmean(scores)} if scores else {},
        metadata={"arm": arm, **extra_metadata},
        duration_s=duration_s,
        results=results,
    )


async def persist_skill_replay_summary(
    db,
    report: SkillReplayReport,
    *,
    duration_s: float = 0.0,
) -> tuple[str, str]:
    """Write the paired OLD/NEW ``eval_runs`` rows; returns (control_id, treatment_id).

    Mutates ``report.control_run_id`` / ``report.treatment_run_id`` in place.
    """
    from genesis.eval.db import insert_run, set_comparison_run

    extra = _verdict_metadata(report)
    control_id = await insert_run(
        db, _arm_summary(report, ARM_OLD, f"{report.run_id}-old", duration_s, extra)
    )
    treatment_id = await insert_run(
        db,
        _arm_summary(
            report,
            ARM_NEW,
            f"{report.run_id}-new",
            duration_s,
            {**extra, "paired_run_id": control_id},
        ),
    )
    await set_comparison_run(db, treatment_id, control_id)  # commits
    report.control_run_id = control_id
    report.treatment_run_id = treatment_id
    return control_id, treatment_id


async def log_replay_observation(db, report: SkillReplayReport, *, now: str) -> str:
    """Log the recommend-only verdict as an observation. Returns the obs id."""
    from genesis.db.crud import observations

    v = report.verdict
    verdict_label = v.verdict if v else VERDICT_INCONCLUSIVE
    content = {
        "skill_name": report.skill_name,
        "verdict": verdict_label,
        "n_complete": v.n_complete if v else 0,
        "n_regressions": v.n_regressions if v else 0,
        "n_improvements": v.n_improvements if v else 0,
        "note": v.note if v else "",
        "run_id": report.run_id,
        "control_run_id": report.control_run_id,
        "treatment_run_id": report.treatment_run_id,
        "task_set_version": report.task_set_version,
        "rubric_version": report.rubric_version,
    }
    obs_id = str(uuid.uuid4())
    await observations.create(
        db,
        id=obs_id,
        source="skill_evolution_gate",
        type="skill_replay_verdict",
        content=json.dumps(content),
        priority="high" if verdict_label == VERDICT_REGRESSION else "low",
        created_at=now,
    )
    return obs_id
