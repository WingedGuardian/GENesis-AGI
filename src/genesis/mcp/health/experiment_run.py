"""experiment_run MCP tool — Crucible's "run" button.

The action surface the experimentation harness ("Crucible") was missing: it
runs a control-vs-treatment **reflection-prompt** A/B over the calibrated golden
set via ``run_reflection_experiment``, **persists** the result to ``eval_runs``
(trigger='experiment'), and returns the win-rate verdict.

Recommend-only: this MEASURES a prompt variant and records the result — it
NEVER promotes one. ``autonomous_action`` is always False; a human acts on the
recommendation. Surfaced afterward via ``experiment_status``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Default judge is a healthy free provider, NOT the runner module's stale
# ``nvidia-nim-deepseek`` default (which is frequently down); both are
# caller-overridable.
_DEFAULT_GEN_PROVIDER = "groq-free"
_DEFAULT_JUDGE_PROVIDER = "groq-free"


async def _impl_experiment_run(
    *,
    experiment_name: str,
    control_prompt: str,
    treatment_prompt: str,
    control_name: str = "control",
    treatment_name: str = "treatment",
    golden_set_path: str | None = None,
    limit: int | None = None,
    gen_provider: str = _DEFAULT_GEN_PROVIDER,
    judge_provider: str = _DEFAULT_JUDGE_PROVIDER,
) -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = getattr(health_mcp_mod, "_service", None)
    if _service is None or getattr(_service, "_db", None) is None:
        return {"status": "unavailable", "message": "DB not initialized"}
    db = _service._db

    if golden_set_path:
        gs_path = Path(golden_set_path)
    else:
        from genesis.eval.reflection_golden_set import DEFAULT_OUTPUT

        gs_path = DEFAULT_OUTPUT
    if not gs_path.exists():
        return {
            "status": "error",
            "message": (
                f"golden set not found: {gs_path}. Generate it with: "
                "python -m genesis.eval.reflection_golden_set --count 150"
            ),
        }

    from genesis.experimentation import runner as _runner
    from genesis.experimentation.types import CognitiveVariant

    control = CognitiveVariant(
        name=control_name, description="control arm", system_prompt=control_prompt,
    )
    treatment = CognitiveVariant(
        name=treatment_name, description="treatment arm", system_prompt=treatment_prompt,
    )

    try:
        result = await _runner.run_reflection_experiment(
            experiment_name=experiment_name,
            control=control,
            treatment=treatment,
            golden_set_path=gs_path,
            gen_provider=gen_provider,
            judge_provider=judge_provider,
            limit=limit,
            db=db,
        )
    except Exception as exc:  # noqa: BLE001 — surface failure, never crash the tool
        logger.warning("experiment_run %s failed", experiment_name, exc_info=True)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    wr = result.winrate or {}
    return {
        "status": "ok",
        "autonomous_action": False,
        "experiment": experiment_name,
        "recommendation": wr.get("recommendation"),
        "significant": wr.get("significant"),
        "winrate": wr,
        "n_cases": result.n_cases,
        "errors": result.errors,
        "control": {
            "variant": result.control.variant_name,
            "mean_score": result.control.mean_score,
            "n_pass": result.control.n_pass,
        },
        "treatment": {
            "variant": result.treatment.variant_name,
            "mean_score": result.treatment.mean_score,
            "n_pass": result.treatment.n_pass,
        },
        "persisted": True,
        "gen_provider": gen_provider,
        "judge_provider": judge_provider,
        "note": (
            "Recommend-only: this MEASURES a reflection-prompt A/B and persists "
            "it to eval_runs (surfaced by experiment_status). It NEVER promotes "
            "a variant. recommendation ∈ {treatment_wins, control_wins, "
            "no_difference, insufficient_data}."
        ),
    }


@mcp.tool()
async def experiment_run(
    experiment_name: str,
    control_prompt: str,
    treatment_prompt: str,
    control_name: str = "control",
    treatment_name: str = "treatment",
    golden_set_path: str = "",
    limit: int = 0,
    gen_provider: str = _DEFAULT_GEN_PROVIDER,
    judge_provider: str = _DEFAULT_JUDGE_PROVIDER,
) -> dict:
    """Run a reflection-prompt A/B (control vs treatment) and persist the result.

    Generates a fresh reflection per golden-set case for each prompt, judges
    both with the ``reflection_quality`` rubric, computes a paired win-rate
    (McNemar exact), and writes the result to ``eval_runs`` (trigger='experiment').
    RECOMMEND-ONLY — nothing is promoted; ``autonomous_action`` is always False.

    Args:
        experiment_name: Label stored with the result.
        control_prompt / treatment_prompt: the two reflection system prompts.
        golden_set_path: JSONL golden set (default: the reflection golden set).
        limit: cap golden cases (0 = all) — use a small N for a cheap smoke run.
        gen_provider / judge_provider: routing-config provider names (default a
            free provider; override to whatever is currently healthy).
    """
    return await _impl_experiment_run(
        experiment_name=experiment_name,
        control_prompt=control_prompt,
        treatment_prompt=treatment_prompt,
        control_name=control_name,
        treatment_name=treatment_name,
        golden_set_path=golden_set_path or None,
        limit=limit or None,
        gen_provider=gen_provider,
        judge_provider=judge_provider,
    )
