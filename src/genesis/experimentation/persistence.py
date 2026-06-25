"""Persist a reflection A/B experiment into ``eval_runs`` (two linked rows).

Reuses the existing ``eval_runs`` table — NO new table, no migration. One row
per arm; the treatment row links to its control via ``comparison_run_id`` (the
architect's reuse). The treatment row's ``metadata_json`` carries the win-rate
+ recommendation — the recommend-only surface the human reads via the
``experiment_status`` MCP tool. Run-level only (no per-case ``eval_results``);
per-case persistence is a follow-up.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from genesis.eval.db import insert_run, set_comparison_run
from genesis.eval.types import EvalRunSummary, EvalTrigger, TaskCategory

if TYPE_CHECKING:
    import aiosqlite

    from genesis.experimentation.types import ExperimentResult


def _arm_summary(
    *, run_id: str, model_id: str, dataset: str, n: int, n_pass: int,
    mean_score: float, metadata: dict,
) -> EvalRunSummary:
    return EvalRunSummary(
        run_id=run_id,
        model_id=model_id,
        model_profile=str(metadata.get("arm", "")),
        dataset=dataset,
        trigger=EvalTrigger.EXPERIMENT,
        task_category=TaskCategory.REASONING,  # reflection quality ~ reasoning
        total_cases=n,
        passed_cases=n_pass,
        failed_cases=n - n_pass,
        aggregate_score=mean_score,
        metadata=metadata,
        results=[],
    )


async def persist_experiment(
    db: aiosqlite.Connection,
    result: ExperimentResult,
    *,
    gen_provider: str,
    judge_provider: str,
) -> dict[str, str]:
    """Write the control + treatment arms as two linked ``eval_runs`` rows.

    Returns ``{"control_run_id": ..., "treatment_run_id": ...}``.
    """
    dataset = f"experiment:reflection:{result.experiment_name}"
    control_id = uuid.uuid4().hex
    treatment_id = uuid.uuid4().hex
    n = result.n_cases

    await insert_run(db, _arm_summary(
        run_id=control_id, model_id=gen_provider, dataset=dataset, n=n,
        n_pass=result.control.n_pass, mean_score=result.control.mean_score,
        metadata={"arm": "control", "variant": result.control.variant_name},
    ))
    await insert_run(db, _arm_summary(
        run_id=treatment_id, model_id=gen_provider, dataset=dataset, n=n,
        n_pass=result.treatment.n_pass, mean_score=result.treatment.mean_score,
        metadata={
            "arm": "treatment",
            "variant": result.treatment.variant_name,
            "winrate": result.winrate,
            "recommendation": result.winrate.get("recommendation"),
            "judge_provider": judge_provider,
            "control_run_id": control_id,
            "experiment": result.metadata,
            "errors": result.errors,
        },
    ))
    await set_comparison_run(db, treatment_id, control_id)
    return {"control_run_id": control_id, "treatment_run_id": treatment_id}
