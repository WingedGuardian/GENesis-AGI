"""Offline reflection-prompt A/B runner — Phase 7 Target 1 (the engine proof).

Runs a CONTROL vs TREATMENT reflection *prompt* variant over a calibrated
golden set: for each golden case, generate a fresh reflection per arm from the
case's stored ``session_context``, judge each with the ``reflection_quality``
rubric, and compute a paired win-rate.

Discipline:
- **No live runtime, no side effects on cognition.** Generation + judging go
  through a standalone litellm router; nothing writes to `observations`,
  `memory`, or any cognitive table. The only optional persistence is an
  `eval_runs`/`eval_results` record (observability), added separately.
- **Single-shot, by design.** Live *deep* reflection is an agentic CC session;
  this offline harness renders a single completion. That is sufficient to prove
  the experiment *machinery* (variant -> generate -> judge -> win-rate). Realistic
  agentic-reflection experiments are future work.
- **Recommend-only.** The output is a win-rate + recommendation; nothing is
  promoted.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from genesis.eval.calibration import _load_golden_set
from genesis.eval.scorers import LLMJudgeScorer
from genesis.eval.stats import compute_score_winrate, compute_winrate
from genesis.experimentation.standalone_router import (
    DEFAULT_GEN_PROVIDER,
    DEFAULT_JUDGE_PROVIDER,
    StandaloneLiteLLMRouter,
    _default_config_path,
)
from genesis.experimentation.types import ArmResult, CognitiveVariant, ExperimentResult
from genesis.routing.config import load_config
from genesis.routing.litellm_delegate import LiteLLMDelegate

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _extract_reflection_text(raw: str) -> str:
    """Reduce a deep-reflection JSON output to a single scoreable text.

    The deep prompt emits JSON with an ``observations`` array; the rubric scores
    one reflection text, so join the observations. Robust to markdown fences and
    prose-wrapped JSON; falls back to the raw string if it isn't the expected
    shape (the judge then scores whatever the model produced — fair across arms).
    """
    if not raw:
        return ""
    # Try to locate a JSON object even if wrapped in prose / fences.
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            obs = data.get("observations")
            if isinstance(obs, list) and obs:
                return "\n".join(str(o) for o in obs)
            csu = data.get("cognitive_state_update")
            if isinstance(csu, str) and csu.strip():
                return csu
    return raw.strip()


async def run_reflection_experiment(
    *,
    experiment_name: str,
    control: CognitiveVariant,
    treatment: CognitiveVariant,
    golden_set_path: Path,
    rubric_name: str = "reflection_quality",
    gen_provider: str = DEFAULT_GEN_PROVIDER,
    judge_provider: str = DEFAULT_JUDGE_PROVIDER,
    limit: int | None = None,
    gen_router: object | None = None,
    judge: object | None = None,
    db: object | None = None,
) -> ExperimentResult:
    """Run a control-vs-treatment reflection-prompt A/B over the golden set.

    Args:
        experiment_name: Label for this experiment (stored in result metadata).
        control / treatment: Variants whose ``system_prompt`` is the reflection
            system prompt for that arm.
        golden_set_path: JSONL golden set (reused: each case supplies the
            generation context + the judge's ``scorer_config``).
        rubric_name: Judge rubric (default ``reflection_quality``).
        gen_provider / judge_provider: routing-config provider names for
            generation and judging (used only when gen_router/judge are not
            injected).
        limit: If set, only the first N golden cases (for smoke tests).
        gen_router / judge: Optional injected generation router (with
            ``route_call``) and judge (with ``score_async``) — for testing or
            to reuse a live runtime's router. Built from the providers when
            omitted.

    Returns:
        ExperimentResult with both arms' per-case pass/fail and the win-rate.
    """
    cases = _load_golden_set(golden_set_path)
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise ValueError(f"golden set {golden_set_path} has no cases")

    if not control.system_prompt or not treatment.system_prompt:
        raise ValueError("both control and treatment must set system_prompt for a reflection A/B")

    created_gen = gen_router is None
    if gen_router is None or judge is None:
        config = load_config(_default_config_path())
        delegate = LiteLLMDelegate(config)
        if gen_router is None:
            gen_router = StandaloneLiteLLMRouter(gen_provider, config=config, delegate=delegate)
        if judge is None:
            judge = LLMJudgeScorer(
                router=StandaloneLiteLLMRouter(judge_provider, config=config, delegate=delegate),
            )

    control_scores: list[float] = []
    treatment_scores: list[float] = []
    control_pass: list[bool] = []
    treatment_pass: list[bool] = []
    errors = 0

    async def _score_arm(
        variant: CognitiveVariant, ctx: str, expected: str, scorer_config: dict,
    ) -> tuple[bool, float]:
        nonlocal errors
        messages = [
            {"role": "system", "content": variant.system_prompt},
            {"role": "user", "content": ctx},
        ]
        gen = await gen_router.route_call("reflection_gen", messages)
        if not gen.success or not gen.content:
            errors += 1
            return False, 0.0
        actual = _extract_reflection_text(gen.content)
        passed, score, _detail = await judge.score_async(actual, expected, scorer_config)
        return bool(passed), float(score)

    try:
        for case in cases:
            scorer_config = dict(case.get("scorer_config") or {})
            scorer_config.setdefault("rubric_name", rubric_name)
            ctx = scorer_config.get("session_context", "")
            expected = case.get("expected", "")
            for variant, scores, passes in (
                (control, control_scores, control_pass),
                (treatment, treatment_scores, treatment_pass),
            ):
                try:
                    passed, score = await _score_arm(variant, ctx, expected, scorer_config)
                except Exception:  # noqa: BLE001 — one bad case must not abort the run
                    logger.warning(
                        "experiment %s case %s arm %s failed",
                        experiment_name, case.get("id"), variant.name, exc_info=True,
                    )
                    errors += 1
                    passed, score = False, 0.0
                scores.append(score)
                passes.append(passed)
    finally:
        if created_gen:
            await gen_router.close()

    # Primary signal: continuous score comparison (sensitive to sub-threshold
    # differences). Secondary: pass/fail at the rubric threshold, for context.
    winrate = compute_score_winrate(control_scores, treatment_scores)
    pass_winrate = compute_winrate(control_pass, treatment_pass)
    try:
        from genesis.experimentation.guards import pin_rubric_version

        rubric_version = pin_rubric_version(rubric_name)
    except Exception:  # noqa: BLE001 — version pin is best-effort audit metadata
        rubric_version = None

    n = len(cases)
    result = ExperimentResult(
        experiment_name=experiment_name,
        control=ArmResult(
            variant_name=control.name,
            case_scores=control_scores,
            case_results=control_pass,
            n_pass=sum(control_pass),
            mean_score=round(sum(control_scores) / n, 4) if n else 0.0,
        ),
        treatment=ArmResult(
            variant_name=treatment.name,
            case_scores=treatment_scores,
            case_results=treatment_pass,
            n_pass=sum(treatment_pass),
            mean_score=round(sum(treatment_scores) / n, 4) if n else 0.0,
        ),
        winrate=winrate,
        n_cases=n,
        errors=errors,
        metadata={
            "rubric_name": rubric_name,
            "rubric_version": rubric_version,
            "gen_provider": gen_provider,
            "judge_provider": judge_provider,
            "control_description": control.description,
            "treatment_description": treatment.description,
            "pass_winrate": pass_winrate,
        },
    )

    if db is not None:
        from genesis.experimentation.persistence import persist_experiment

        await persist_experiment(db, result, gen_provider=gen_provider, judge_provider=judge_provider)
    return result
