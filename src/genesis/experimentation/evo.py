"""Evo — the evolution loop on top of Crucible (recommend-only, measure-only).

Fans out N reflection-PROMPT variants, tests each independently vs a shared
control via ``run_reflection_experiment``, keeps only those that beat control at
a **Bonferroni-corrected** threshold (alpha / N), picks the best survivor, then
**re-validates it on a disjoint held-out golden slice** before declaring a
winner. The held-out pass + the corrected threshold are the winner's-curse /
multiple-comparison defenses — they reduce, not eliminate, the inflated
false-positive rate of "pick the best of N". This is acceptable because Evo is
**recommend-only**: it surfaces a recommendation; a human gates any promotion.
It NEVER mutates cognition.

Cost is bounded deterministically by fan-out: at most
``len(candidates) * eval_limit + holdout_limit`` reflection generations (×2 for
judging), on free providers, on demand. (USD accounting is a later refinement —
``run_reflection_experiment`` does not surface per-call cost today.)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from genesis.eval.calibration import _load_golden_set
from genesis.experimentation.types import CognitiveVariant

logger = logging.getLogger(__name__)

_TMP_DIR = os.path.expanduser("~/tmp")


@dataclass(frozen=True)
class EvoConfig:
    eval_limit: int = 30          # golden cases per fan-out experiment (M)
    holdout_limit: int = 20       # disjoint cases for winner re-validation
    alpha: float = 0.05           # family-wise target; per-test gate = alpha / N
    gen_provider: str = "groq-free"
    judge_provider: str = "groq-free"


@dataclass(frozen=True)
class EvoResult:
    winner: CognitiveVariant | None
    winner_winrate: dict[str, Any] | None
    holdout_winrate: dict[str, Any] | None
    candidates_evaluated: int
    survivors: int
    note: str
    holdout_disjoint: bool = True  # False when the golden set was too small to hold out
    per_candidate: list[dict[str, Any]] = field(default_factory=list)


# Fixed mutation directives appended to the base prompt to form variants.
# DIRECTIVE-APPEND (not full LLM rewrite): deterministic, no generation LLM call,
# and zero truncation risk on the large (~13KB) base prompt — the exact pattern
# the Crucible foundation E2E proved (control + an appended steering directive).
# A multi-generation Evo may later LLM-propose novel directives.
_VARIATION_DIRECTIVES = (
    "For this reflection, be more concise and direct; cut filler.",
    "For this reflection, push for deeper, non-obvious patterns rather than surface restatement.",
    "For this reflection, add explicit step-by-step structure to the analysis.",
    "For this reflection, prioritize — surface the single most important thing first, plainly.",
    "For this reflection, be more skeptical: challenge assumptions and hunt for contradictions.",
    "For this reflection, favor concrete, actionable observations over abstract ones.",
)


def build_directive_variants(base_prompt: str, n: int) -> list[CognitiveVariant]:
    """Build up to ``n`` variants by appending a fixed mutation directive to the
    base prompt. Deterministic — NO LLM call, no truncation risk. Each variant is
    ``base_prompt`` + one directive (capped at the number of directives)."""
    variants: list[CognitiveVariant] = []
    for i in range(min(max(0, n), len(_VARIATION_DIRECTIVES))):
        directive = _VARIATION_DIRECTIVES[i]
        variants.append(CognitiveVariant(
            name=f"evo_v{i}",
            description=directive,
            system_prompt=f"{base_prompt}\n\n{directive}",
        ))
    return variants


def _write_slice(cases: list[dict], suffix: str) -> Path:
    os.makedirs(_TMP_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=f"_{suffix}.jsonl", dir=_TMP_DIR, delete=False,
    ) as fh:
        for case in cases:
            fh.write(json.dumps(case) + "\n")
        return Path(fh.name)


def _split_golden_set(
    path: Path, eval_limit: int, holdout_limit: int,
) -> tuple[Path, Path, bool]:
    """Split the golden set into a fan-out slice (first ``eval_limit``) and a
    held-out slice (the next ``holdout_limit``). Returns ``(eval_path,
    holdout_path, disjoint)`` — ``disjoint`` is False when the golden set was too
    small to hold out and the held-out slice reuses the eval tail (degraded
    winner's-curse defense; surfaced to the caller, not hidden)."""
    cases = _load_golden_set(path)
    if not cases:
        raise ValueError(f"golden set {path} has no cases")
    eval_cases = cases[:eval_limit]
    holdout_cases = cases[eval_limit:eval_limit + holdout_limit]
    disjoint = bool(holdout_cases)
    if not holdout_cases:
        logger.warning(
            "evo: golden set %s too small for a disjoint held-out slice "
            "(have %d, eval_limit=%d) — re-validating on the eval tail (NOT disjoint)",
            path, len(cases), eval_limit,
        )
        holdout_cases = eval_cases[-max(1, holdout_limit):]
    return (
        _write_slice(eval_cases, "eval"),
        _write_slice(holdout_cases, "holdout"),
        disjoint,
    )


def _passes_gate(winrate: dict, threshold: float) -> bool:
    p = winrate.get("p_value")
    return (
        winrate.get("recommendation") == "treatment_wins"
        and p is not None
        and p <= threshold
    )


async def run_evo(
    *,
    base: CognitiveVariant,
    candidates: list[CognitiveVariant],
    golden_set_path: Path,
    config: EvoConfig | None = None,
    gen_router: object | None = None,
    judge: object | None = None,
) -> EvoResult:
    """Run one Evo generation over prompt ``candidates`` vs ``base``.

    Recommend-only: returns the best variant that beats control at alpha/N AND
    survives held-out re-validation, or ``winner=None``. Mutates nothing.
    """
    if config is None:
        config = EvoConfig()
    n = len(candidates)
    if n == 0:
        return EvoResult(
            winner=None, winner_winrate=None, holdout_winrate=None,
            candidates_evaluated=0, survivors=0, note="no candidates",
        )

    from genesis.experimentation import runner as _runner

    eval_path, holdout_path, holdout_disjoint = _split_golden_set(
        golden_set_path, config.eval_limit, config.holdout_limit,
    )
    threshold = config.alpha / n  # Bonferroni
    survivors: list[tuple[CognitiveVariant, Any]] = []
    per_candidate: list[dict[str, Any]] = []
    try:
        for cand in candidates:
            try:
                res = await _runner.run_reflection_experiment(
                    experiment_name=f"evo:{cand.name}",
                    control=base,
                    treatment=cand,
                    golden_set_path=eval_path,
                    gen_provider=config.gen_provider,
                    judge_provider=config.judge_provider,
                    limit=config.eval_limit,
                    gen_router=gen_router,
                    judge=judge,
                )
            except Exception:  # noqa: BLE001 — one bad variant must not abort the run
                logger.warning("evo: variant %s failed", cand.name, exc_info=True)
                per_candidate.append({"variant": cand.name, "error": True})
                continue
            wr = res.winrate or {}
            survived = _passes_gate(wr, threshold)
            per_candidate.append({
                "variant": cand.name,
                "recommendation": wr.get("recommendation"),
                "p_value": wr.get("p_value"),
                "mean_score": res.treatment.mean_score,
                "survived_gate": survived,
            })
            if survived:
                survivors.append((cand, res))

        if not survivors:
            return EvoResult(
                winner=None, winner_winrate=None, holdout_winrate=None,
                candidates_evaluated=n, survivors=0,
                note=f"no variant beat control at alpha/N={threshold:.4f}",
                holdout_disjoint=holdout_disjoint,
                per_candidate=per_candidate,
            )

        best_cand, best_res = max(survivors, key=lambda cr: cr[1].treatment.mean_score)

        # Winner's-curse defense: re-validate the best on the disjoint held-out slice.
        try:
            hold = await _runner.run_reflection_experiment(
                experiment_name=f"evo:holdout:{best_cand.name}",
                control=base,
                treatment=best_cand,
                golden_set_path=holdout_path,
                gen_provider=config.gen_provider,
                judge_provider=config.judge_provider,
                limit=config.holdout_limit,
                gen_router=gen_router,
                judge=judge,
            )
            hwr = hold.winrate or {}
        except Exception:  # noqa: BLE001
            logger.warning("evo: held-out re-validation failed", exc_info=True)
            hwr = {}

        # Held-out re-validation is a SINGLE comparison → gate on the uncorrected
        # alpha explicitly (not the baked-in `significant`, which is also α=0.05
        # but would silently diverge if the fan-out's α/N is read as the bar).
        hp = hwr.get("p_value")
        confirmed = (
            hwr.get("recommendation") == "treatment_wins"
            and hp is not None
            and hp <= config.alpha
        )
        winner = best_cand if confirmed else None
        note = (
            f"winner {best_cand.name} confirmed on held-out slice"
            if confirmed
            else f"best survivor {best_cand.name} did NOT survive held-out re-validation"
        )
        if confirmed and not holdout_disjoint:
            note += " (WARNING: held-out slice was NOT disjoint — golden set too small)"
        return EvoResult(
            winner=winner,
            winner_winrate=best_res.winrate,
            holdout_winrate=hwr,
            candidates_evaluated=n,
            survivors=len(survivors),
            note=note,
            holdout_disjoint=holdout_disjoint,
            per_candidate=per_candidate,
        )
    finally:
        for p in (eval_path, holdout_path):
            with contextlib.suppress(OSError):
                p.unlink()


async def persist_evo_summary(
    db: object,
    result: EvoResult,
    *,
    gen_provider: str,
    judge_provider: str,
    label: str,
) -> str:
    """Write ONE ``eval_runs`` summary row for an Evo run (history/audit).

    Reuses ``trigger=EXPERIMENT`` (no enum/migration change) with a
    ``kind='evo_summary'`` metadata marker + an ``evo:reflection:`` dataset, so
    ``experiment_status`` surfaces it under ``evo_runs`` without polluting the
    2-arm experiments list. Run-level summary only (winner + survivors +
    per-candidate scores) — NOT the N+1 fan-out rows.
    """
    import uuid as _uuid

    from genesis.eval.db import insert_run
    from genesis.eval.types import EvalRunSummary, EvalTrigger, TaskCategory

    run_id = _uuid.uuid4().hex
    w = result.winner
    winner_mean = (result.winner_winrate or {}).get("treatment_mean_score") or 0.0
    await insert_run(db, EvalRunSummary(
        run_id=run_id,
        model_id=judge_provider,
        model_profile="evo_summary",
        dataset=f"evo:reflection:{label}",
        trigger=EvalTrigger.EXPERIMENT,
        task_category=TaskCategory.REASONING,
        total_cases=result.candidates_evaluated,
        passed_cases=result.survivors,
        failed_cases=max(0, result.candidates_evaluated - result.survivors),
        aggregate_score=winner_mean,
        metadata={
            "kind": "evo_summary",
            "winner": (w.name if w else None),
            "winner_approach": (w.description if w else None),
            "survivors": result.survivors,
            "candidates_evaluated": result.candidates_evaluated,
            "holdout_disjoint": result.holdout_disjoint,
            "winner_winrate": result.winner_winrate,
            "holdout_winrate": result.holdout_winrate,
            "per_candidate": result.per_candidate,
            "note": result.note,
            "gen_provider": gen_provider,
            "judge_provider": judge_provider,
        },
        results=[],
    ))
    return run_id
