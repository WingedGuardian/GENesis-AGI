"""MODEL_EVAL surplus task executor.

Reads the model_id from the task payload, runs eval against all
available datasets, and produces an observation with the results.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from genesis.eval.datasets import list_datasets
from genesis.eval.runner import run_eval
from genesis.eval.types import EvalTrigger
from genesis.surplus.types import ExecutorResult, SurplusTask

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


class ModelEvalExecutor:
    """Surplus executor for MODEL_EVAL tasks.

    Payload format (JSON):
        {"model_id": "cerebras-qwen", "datasets": ["classification"]}

    If datasets is omitted, runs all available datasets.
    """

    def __init__(self, *, db: aiosqlite.Connection | None = None) -> None:
        self._db = db

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        # Parse payload
        payload = _parse_payload(task.payload)
        provider_name = payload.get("model_id")
        if not provider_name:
            return ExecutorResult(
                success=False,
                error="MODEL_EVAL task missing 'model_id' in payload",
            )

        requested_datasets = payload.get("datasets") or list_datasets()
        if not requested_datasets:
            return ExecutorResult(
                success=False,
                error="no eval datasets available",
            )

        # Run eval for each dataset
        summaries = []
        errors = []
        for ds_name in requested_datasets:
            try:
                summary = await run_eval(
                    provider_name=provider_name,
                    dataset_name=ds_name,
                    trigger=EvalTrigger.SURPLUS,
                    db=self._db,
                )
                summaries.append(summary)
            except Exception as exc:
                logger.warning(
                    "MODEL_EVAL failed for %s/%s: %s",
                    provider_name, ds_name, exc,
                )
                errors.append(f"{ds_name}: {exc}")

        if not summaries:
            return ExecutorResult(
                success=False,
                error=f"all eval datasets failed: {'; '.join(errors)}",
            )

        # Build observation content
        lines = [f"Model evaluation: {provider_name}"]
        for s in summaries:
            pct = s.aggregate_score * 100
            lines.append(
                f"  {s.dataset}: {s.passed_cases}/{s.total_cases} "
                f"({pct:.0f}%) in {s.duration_s:.1f}s"
            )
        if errors:
            lines.append(f"  Errors: {'; '.join(errors)}")

        content = "\n".join(lines)

        # Build insight for surplus system
        overall_pass = all(s.aggregate_score >= 0.7 for s in summaries)
        insights = [{
            "content": content,
            "source_task_type": task.task_type,
            "generating_model": provider_name,
            "drive_alignment": task.drive_alignment,
            "confidence": min(s.aggregate_score for s in summaries),
            "eval_passed": overall_pass,
            "provider": provider_name,
        }]

        return ExecutorResult(
            success=True,
            content=content,
            insights=insights,
        )


def _parse_payload(payload: str | None) -> dict:
    """Parse task payload (JSON string or None)."""
    if not payload:
        return {}
    try:
        obj = json.loads(payload)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}
