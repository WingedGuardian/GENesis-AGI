"""Measurement-only quality judge for autonomous surplus insight tasks.

Closes the surplus half of the self-learning loop's *measure* layer. Background
insight tasks (brainstorms, audits, research) previously recorded only "did it
run to completion" — a signal that is ~99.6% positive and therefore carries no
discriminative information about whether the autonomous work was actually *good*.
(The prior intake-based verdict was structurally unreachable: curated surplus
sources skip LLM scoring and route at a fixed 0.6 confidence, so intake never
discarded everything, so a 'hollow' verdict could never fire.)

This module grades the FULL insight output with the existing eval LLM-judge
(:class:`genesis.eval.scorers.OutputQualityScorer` + the ``output_quality``
rubric) and maps the verdict onto the ``surplus_tasks.outcome_quality`` column
that the Outcome Bus harvester already understands::

    pass (score >= rubric threshold)  -> 'useful'
    fail (score <  rubric threshold)  -> 'hollow'   (harvested as VERIFICATION_FAILED)
    judge unavailable / unknown type  -> None        (no verdict; positive-only)

**Measurement-only.** This does NOT change intake routing or gate anything. A
curated source is still trusted for storage; the judge only *observes* quality so
the bus gains a real two-sided signal. Design note: an earlier ``output_quality``
*gate* in the ego pipeline was removed as "fragile under judge-provider outages",
so this is deliberately never a gate — any judge outage yields a NULL verdict for
that task (exactly like an intake failure), never a false negative.

:func:`run_quality_judge` NEVER raises: a judge problem must not fail a surplus
task that otherwise completed.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from genesis.surplus.types import TaskType

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Per-task-type description of what a GOOD output looks like — the ``expected``
# the judge grades ``actual`` against. The Step 0 de-risk probe showed a GENERIC
# expected prompt inflates the fail rate (it graded a structured model record as
# "not a prose insight"), so each type gets a tailored intent string describing
# its legitimate output shape. Only the 11 INSIGHT_PRODUCING_TASK_TYPES appear
# here; any other task type -> no verdict (None).
#
# CALIBRATION NOTE: these strings are ~75%-confidence first drafts. LC3-B go-live
# (consuming this signal to *change behaviour* via the capability aggregator)
# stays gated on ``calibration_by_domain(source='surplus', tier=1)`` over >=30
# graded tasks matching human intuition — validate/tune these before any flag
# flip. Keep the keys in lockstep with surplus.types.INSIGHT_PRODUCING_TASK_TYPES.
_JUDGE_EXPECTED: dict[TaskType, str] = {
    TaskType.BRAINSTORM_USER: (
        "A brainstormed idea or suggestion relevant to the user's goals, projects, "
        "or stated interests. Good output is a specific, actionable, on-topic idea "
        "(or small set of ideas) with enough substance to act on — not vague "
        "platitudes or generic advice."
    ),
    TaskType.BRAINSTORM_SELF: (
        "A self-improvement idea for the Genesis system itself — a concrete "
        "capability, workflow, or architectural improvement. Good output is "
        "specific and grounded in how Genesis actually works, not generic filler."
    ),
    TaskType.META_BRAINSTORM: (
        "An insight about Genesis's own ideation process — how to generate better "
        "ideas or steer its own cognition. Good output is a concrete, reasoned "
        "observation, not a restatement of the prompt."
    ),
    TaskType.MEMORY_AUDIT: (
        "A finding from auditing the memory system: a specific gap, inconsistency, "
        "duplication, or health issue (or a well-supported 'healthy' conclusion). "
        "Good output names concrete items or patterns, not vague generalities."
    ),
    TaskType.PROCEDURE_AUDIT: (
        "A finding from auditing stored procedures: a specific stale, duplicate, "
        "missing, or low-quality procedure with reasoning. Good output is concrete "
        "and references actual procedures or patterns."
    ),
    TaskType.GAP_CLUSTERING: (
        "A clustered analysis of knowledge or capability gaps — coherent themes "
        "grouped from underlying signals, each described specifically. Good output "
        "is a structured set of real, distinguishable gap clusters, not a flat "
        "generic list."
    ),
    TaskType.SELF_UNBLOCK: (
        "Concrete steps or a diagnosis to unblock a stuck goal or task. Good output "
        "identifies the actual blocker and proposes specific, actionable next steps."
    ),
    TaskType.ANTICIPATORY_RESEARCH: (
        "A research finding on a topic likely to become relevant, synthesized from "
        "sources. Good output is substantive, on-topic, and informative — a real "
        "finding or synthesis, not a raw data dump, a list of search queries, or an "
        "off-topic tangent."
    ),
    TaskType.PROMPT_EFFECTIVENESS_REVIEW: (
        "An assessment of how effective a prompt or call-site is, with specific "
        "observations and improvement recommendations. Good output is a concrete, "
        "evidence-based review, not a generic 'looks fine'."
    ),
    TaskType.CODE_AUDIT: (
        "A code-quality, security, or architecture finding with specifics — the "
        "file/area, the issue, and why it matters (or a well-supported 'no issues "
        "found'). Good output is concrete and technically grounded, not vague."
    ),
    TaskType.WING_AUDIT: (
        "A memory-taxonomy hygiene finding about wings/rooms — miscategorization, "
        "sparse or overloaded domains, or structural improvements. Good output "
        "names specific wings/rooms and concrete issues."
    ),
}


async def run_quality_judge(
    content: str,
    task_type: TaskType | str,
    router: Router | None,
) -> tuple[str | None, float | None, str | None]:
    """Grade a completed surplus insight's output; return the verdict.

    Returns ``(outcome_quality, judge_score, judge_detail)`` where:

    - ``outcome_quality`` is ``'useful'`` (judge passed), ``'hollow'`` (judge
      failed → harvested as a VERIFICATION_FAILED negative), or ``None`` (no
      verdict: missing router, unknown/non-insight task type, or judge outage).
      Only ``'hollow'`` becomes a bus negative; ``None`` stays positive-only.
    - ``judge_score`` is the continuous quality score in ``[0, 1]`` (for
      calibration/display; NOT read by the harvester), or ``None``.
    - ``judge_detail`` is the judge's JSON detail (rationale etc.), or ``None``.

    NEVER raises. Every failure path yields ``(None, None, None)`` so a judge
    problem cannot fail an otherwise-completed surplus task.
    """
    if router is None:
        # No router wired (degraded init) — cannot judge; positive-only.
        return None, None, None

    # StrEnum: accept either the enum member or its string value. An unknown
    # value (or a non-insight type absent from _JUDGE_EXPECTED) -> no verdict.
    try:
        tt = TaskType(task_type)
    except ValueError:
        return None, None, None

    expected = _JUDGE_EXPECTED.get(tt)
    if expected is None:
        return None, None, None

    try:
        from genesis.eval.scorers import OutputQualityScorer

        scorer = OutputQualityScorer(router=router)
        passed, score, detail = await scorer.score_async(
            actual=content,
            expected=expected,
            config={"rubric_name": "output_quality"},
        )
    except Exception:
        # Judge infra failure — treat exactly like an intake failure: NULL.
        logger.warning(
            "surplus quality judge raised for task_type=%s — recording NULL verdict",
            tt.value,
            exc_info=True,
        )
        return None, None, None

    # OUTAGE GUARD (critical): score_async signals judge-call and parse failures
    # by returning passed=False with an ``"error"`` key in the detail JSON (see
    # eval.scorers.LLMJudgeScorer.score_async — _JUDGE_CALL_FAIL / _JUDGE_PARSE_FAIL).
    # Without this guard a provider outage would write 'hollow' for EVERY task — a
    # false-negative flood that would poison the Outcome Bus. Match on the presence
    # of the "error" key (not specific sentinel strings) so new error kinds stay
    # covered. Any "error" key => no verdict (NULL).
    try:
        parsed = json.loads(detail) if detail else {}
    except (json.JSONDecodeError, ValueError):
        parsed = {}
    # Only a dict can carry the judge's "error" sentinel. Normalize any non-dict
    # JSON (a list/scalar — not a shape score_async produces today, but guard
    # against a future error format silently bypassing this check) to {} so the
    # membership test below is meaningful; absent an error dict, the passed/score
    # verdict stands (they are score_async's authoritative signal).
    if not isinstance(parsed, dict):
        parsed = {}
    if "error" in parsed:
        logger.info(
            "surplus quality judge unavailable (%s) for task_type=%s — NULL verdict",
            parsed.get("error"),
            tt.value,
        )
        return None, None, None

    outcome_quality = "useful" if passed else "hollow"
    logger.info(
        "surplus quality judge: task_type=%s score=%.3f verdict=%s",
        tt.value,
        score,
        outcome_quality,
    )
    return outcome_quality, score, detail
