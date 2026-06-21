"""Judge-hack guards for the cognitive experimentation harness (Phase 7).

A reflection A/B is scored by a live LLM judge, so the result is only as
trustworthy as the judge. Three guards defend the eval layer against gaming
(reward-hacking the judge rather than producing genuinely better reflections):

1. **rubric-version pinning** — both arms scored by the same ``Rubric.version``,
   recorded in the result so a silent rubric edit can't masquerade as a win.
2. **calibration-required** — the rubric must clear the ≥0.80 human-agreement
   ship-gate (``eval/calibration.py``) before its A/B verdicts are trusted.
3. **held-out second judge** — re-run the A/B with a *different* judge provider;
   a directional "win" that doesn't survive the held-out judge is flagged
   ``judge_overfit`` (the win was the judge's bias, not real quality).

All recommend-only: guards inform the human, they never auto-promote or block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genesis.eval.calibration import DEFAULT_AGREEMENT_THRESHOLD
from genesis.experimentation.standalone_router import DEFAULT_JUDGE_PROVIDER

if TYPE_CHECKING:
    from pathlib import Path

_DIRECTIONAL_WINS = frozenset({"treatment_wins", "control_wins"})


def pin_rubric_version(rubric_name: str) -> str:
    """Return the rubric's semantic version (stored with the A/B for audit)."""
    from genesis.eval.rubrics import get_rubric

    return get_rubric(rubric_name).version


def held_out_verdict(primary_recommendation: str, heldout_recommendation: str) -> dict:
    """Does the primary judge's directional win survive a held-out judge?

    A non-directional primary recommendation (no_difference / insufficient_data)
    has no win to validate -> survives. A directional win survives only if the
    held-out judge reaches the SAME recommendation; otherwise it's flagged
    ``judge_overfit``.
    """
    if primary_recommendation not in _DIRECTIONAL_WINS:
        return {"survives": True, "flag": None, "reason": "no directional win to validate"}
    survives = heldout_recommendation == primary_recommendation
    return {
        "survives": survives,
        "flag": None if survives else "judge_overfit",
        "reason": (
            "held-out judge agrees"
            if survives
            else f"primary={primary_recommendation}, held-out={heldout_recommendation}"
        ),
    }


async def check_rubric_calibrated(
    rubric_name: str,
    golden_set_path: Path,
    *,
    judge_provider: str = DEFAULT_JUDGE_PROVIDER,
    threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
    router: object | None = None,
) -> dict:
    """Run the rubric against its golden set; report whether it clears the gate.

    The operator runs this once per (rubric, judge) before trusting A/B verdicts.
    Reuses ``eval/calibration.py:run_calibration`` (judge.passed vs user_passed
    agreement). ``router`` is injectable for tests; otherwise a standalone
    litellm router for ``judge_provider`` is used.
    """
    from genesis.eval.calibration import run_calibration

    own = router is None
    if router is None:
        from genesis.experimentation.standalone_router import StandaloneLiteLLMRouter

        router = StandaloneLiteLLMRouter(judge_provider)
    try:
        result = await run_calibration(
            rubric=rubric_name,
            golden_set_path=golden_set_path,
            router=router,
            threshold=threshold,
        )
    finally:
        if own:
            await router.close()

    return {
        "calibrated": result.threshold_met,
        "agreement_rate": round(result.agreement_rate, 4),
        "threshold": threshold,
        "n_cases": result.total_cases,
        "rubric_version": result.rubric_version,
        "judge_provider": judge_provider,
    }
