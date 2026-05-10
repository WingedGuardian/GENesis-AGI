"""Rubric calibration — agreement % vs a hand-graded golden set.

A rubric is **calibrated** when the judge model's pass/fail decisions
agree with the user's hand-graded labels on at least
``DEFAULT_AGREEMENT_THRESHOLD`` of cases. Until calibrated, the rubric
should not be wired into live scoring (eval cadences, j9 batches,
ego drift panels) — uncalibrated rubrics produce noise, not signal.

Golden set format (JSONL, one case per line):

    {"id": "case_001",
     "actual": "<the recalled memory text the judge sees>",
     "expected": "<user's reference label, e.g. 'relevant'>",
     "user_passed": true,
     "scorer_config": {"rubric_name": "memory_recall_grounding",
                       "query": "<the original query>"}}

``user_passed`` is the load-bearing field: True if the user judged this
case as a pass for the rubric, False otherwise. ``expected`` is passed
through to the prompt as a reference label but is NOT used to compute
agreement (we compare judge.passed vs user_passed, not judge_score vs
user_score — see ``CalibrationResult`` docstring for why).

The output is a ``CalibrationResult`` dataclass and (optionally) a
markdown report at the path provided by the caller.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.eval.rubrics import Rubric, get_rubric
from genesis.eval.scorers import LLMJudgeScorer

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Default ship-gate. A rubric below this agreement rate is not allowed
# into live scoring.
DEFAULT_AGREEMENT_THRESHOLD = 0.80


@dataclass(frozen=True)
class CalibrationCaseOutcome:
    """Per-case result from a calibration run."""

    case_id: str
    user_passed: bool
    judge_passed: bool
    judge_score: float
    agreed: bool
    rationale: str
    error: str | None = None


@dataclass(frozen=True)
class CalibrationResult:
    """Aggregate calibration result for a rubric.

    Agreement is computed over judge.passed vs user_passed (binary), not
    over score deltas. The rubric defines a ``pass_threshold`` precisely
    so calibration tunes that threshold against user judgment — comparing
    judge_score to a user-supplied numeric score would be a different
    calibration target (regression rather than classification) and adds
    a confounder we don't need for Phase 1.

    **Known limitation: this is classification agreement, not
    probability calibration.** A judge that returns 0.51 for one case
    and 0.99 for another counts identically toward agreement once both
    cross ``pass_threshold``. The metric is also vulnerable to
    majority-class baselines on imbalanced golden sets: if 80% of the
    set is user_passed=True, a judge that always returns "pass" scores
    80% agreement without doing any real grading. Two mitigations:
    (1) the golden-set scaffold guides users toward a balanced mix of
    clear-positives, clear-negatives, and borderlines; (2) reviewers
    inspect the per-case outcomes (rendered in ``render_report``)
    before promoting a rubric, not just the headline rate.

    ``threshold_met`` is True when ``agreement_rate >= threshold``.
    Errors (judge call failed, parse failed) count as disagreements —
    the rubric isn't safe to ship if the judge can't return well-formed
    output on real inputs.
    """

    rubric_name: str
    rubric_version: str
    judge_call_site: str
    total_cases: int
    agreed_cases: int
    disagreed_cases: int
    error_cases: int
    agreement_rate: float
    threshold: float
    threshold_met: bool
    duration_s: float
    generated_at: str
    outcomes: list[CalibrationCaseOutcome] = field(default_factory=list)


def _load_golden_set(path: Path) -> list[dict]:
    """Read a JSONL golden set. Empty lines and lines starting with ``#``
    are skipped — comments + blank lines keep hand-graded files readable.
    """
    if not path.exists():
        msg = f"golden set not found: {path}"
        raise FileNotFoundError(msg)

    cases: list[dict] = []
    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"{path}:{lineno}: invalid JSON: {exc}"
            raise ValueError(msg) from exc

        for required in ("id", "actual", "user_passed"):
            if required not in case:
                msg = (
                    f"{path}:{lineno}: case missing required field "
                    f"{required!r}"
                )
                raise ValueError(msg)

        cases.append(case)
    return cases


async def run_calibration(
    *,
    rubric: Rubric | str,
    golden_set_path: Path,
    router: Router,
    threshold: float = DEFAULT_AGREEMENT_THRESHOLD,
) -> CalibrationResult:
    """Run ``rubric`` against the golden set and report agreement.

    Args:
        rubric: Either a Rubric instance or a registered rubric name.
        golden_set_path: Path to the JSONL golden set.
        router: Routing dispatcher used to invoke the judge call site.
        threshold: Minimum agreement rate to mark the rubric calibrated.

    Returns:
        A CalibrationResult. Inspect ``threshold_met`` before promoting
        the rubric.

    Raises:
        FileNotFoundError if the golden set file is missing.
        ValueError if the golden set is malformed or empty.
    """
    if isinstance(rubric, str):
        rubric = get_rubric(rubric)

    cases = _load_golden_set(golden_set_path)
    if not cases:
        msg = (
            f"golden set {golden_set_path} contains no graded cases — "
            f"calibration requires at least one case (recommend 30+)"
        )
        raise ValueError(msg)

    scorer = LLMJudgeScorer(router=router)

    t0 = time.monotonic()
    outcomes: list[CalibrationCaseOutcome] = []
    agreed = 0
    disagreed = 0
    errors = 0

    for case in cases:
        case_id = case["id"]
        user_passed = bool(case["user_passed"])
        actual = case["actual"]
        expected = case.get("expected", "")
        scorer_config = dict(case.get("scorer_config", {}))

        # Default rubric_name to the rubric we're calibrating — saves
        # the user from repeating it on every line.
        scorer_config.setdefault("rubric_name", rubric.name)

        try:
            judge_passed, judge_score, detail_json = await scorer.score_async(
                actual, expected, scorer_config,
            )
        except Exception as exc:
            logger.warning(
                "Calibration case %s raised: %s", case_id, exc,
            )
            errors += 1
            outcomes.append(CalibrationCaseOutcome(
                case_id=case_id,
                user_passed=user_passed,
                judge_passed=False,
                judge_score=0.0,
                agreed=False,
                rationale="",
                error=str(exc),
            ))
            continue

        try:
            detail = json.loads(detail_json)
        except json.JSONDecodeError:
            detail = {}

        # Judge call/parse failures count as disagreements — see
        # CalibrationResult docstring.
        if "error" in detail:
            errors += 1
            outcomes.append(CalibrationCaseOutcome(
                case_id=case_id,
                user_passed=user_passed,
                judge_passed=False,
                judge_score=0.0,
                agreed=False,
                rationale="",
                error=detail.get("error_message", detail["error"]),
            ))
            continue

        agreement = judge_passed == user_passed
        if agreement:
            agreed += 1
        else:
            disagreed += 1

        outcomes.append(CalibrationCaseOutcome(
            case_id=case_id,
            user_passed=user_passed,
            judge_passed=judge_passed,
            judge_score=judge_score,
            agreed=agreement,
            rationale=detail.get("rationale", ""),
        ))

    duration_s = time.monotonic() - t0
    total = len(cases)
    agreement_rate = agreed / total if total else 0.0

    return CalibrationResult(
        rubric_name=rubric.name,
        rubric_version=rubric.version,
        judge_call_site="judge",
        total_cases=total,
        agreed_cases=agreed,
        disagreed_cases=disagreed,
        error_cases=errors,
        agreement_rate=agreement_rate,
        threshold=threshold,
        threshold_met=agreement_rate >= threshold,
        duration_s=duration_s,
        generated_at=datetime.now(UTC).isoformat(),
        outcomes=outcomes,
    )


def render_report(result: CalibrationResult) -> str:
    """Render a calibration result as a markdown report.

    Output is intended for ``~/.genesis/output/`` — never the repo tree.
    The report leads with the ship-gate verdict so a quick skim answers
    "can we ship this rubric?"
    """
    verdict = "PROMOTE" if result.threshold_met else "BLOCKED"
    pct = f"{result.agreement_rate * 100:.1f}%"
    threshold_pct = f"{result.threshold * 100:.0f}%"

    lines = [
        f"# Calibration: {result.rubric_name} v{result.rubric_version}",
        "",
        f"**Verdict:** {verdict} "
        f"({pct} agreement, threshold {threshold_pct})",
        "",
        f"- Total cases: {result.total_cases}",
        f"- Agreed: {result.agreed_cases}",
        f"- Disagreed: {result.disagreed_cases}",
        f"- Errors: {result.error_cases}",
        f"- Duration: {result.duration_s:.1f}s",
        f"- Generated: {result.generated_at}",
        "",
    ]

    if result.disagreed_cases or result.error_cases:
        lines.append("## Disagreements & errors")
        lines.append("")
        for o in result.outcomes:
            if o.agreed:
                continue
            label = (
                f"ERROR: {o.error}"
                if o.error
                else f"user={o.user_passed}, judge={o.judge_passed} "
                f"(score={o.judge_score:.2f})"
            )
            lines.append(f"- **{o.case_id}** — {label}")
            if o.rationale:
                lines.append(f"  - Rationale: {o.rationale}")
        lines.append("")

    if result.threshold_met:
        lines.append(
            "Agreement meets the ship gate. Rubric is safe to wire into "
            "live scoring.",
        )
    else:
        lines.append(
            "Agreement is below the ship gate. Iterate the rubric "
            "prompt, expand the golden set, or both — do NOT promote "
            "this rubric until agreement clears the threshold.",
        )

    return "\n".join(lines)
