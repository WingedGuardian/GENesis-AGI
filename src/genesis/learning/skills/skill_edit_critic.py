"""Skill-edit Critic — shadow screen for self-proposed SKILL.md edits.

The skill-evolution pipeline auto-applies MINOR skill edits with only a
*structural* check (``SkillValidator``). This Critic adds a semantic screen for
the failure modes of autonomous self-modification — reward-hacking,
catastrophic forgetting, under-exploration, and constraint-stripping — via the
``skill_edit_regression`` rubric run through the shared ``LLMJudgeScorer``
(``judge`` call site → cost tracking + chain fallback + degrade-to-NULL).

SHADOW BY CONSTRUCTION (WS1): :func:`run_critic` only *computes* a verdict; the
caller (``applicator.apply``) logs it as an observation AFTER the edit has
already been written, and never blocks on it. Never raises — a judge problem
must never disturb the skill-evolution pass. This mirrors the battle-tested
``surplus.quality_judge.run_quality_judge`` wrapper.

Levers: env ``GENESIS_SKILL_EVOLUTION_GATE_OFF`` (hard kill, checked first) and
the ``skill_evolution_gate`` settings domain (``off | shadow``, live-read).
"""

from __future__ import annotations

import difflib
import json
import logging
from typing import TYPE_CHECKING

from genesis.env import skill_gate_off
from genesis.learning.skills.skill_gate_config import skill_gate_mode

if TYPE_CHECKING:
    from genesis.learning.skills.types import SkillProposal
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

_RUBRIC_NAME = "skill_edit_regression"
_RUBRIC_VERSION = "1.0.0"

# Known pathology tokens the rubric may emit. Anything else is dropped so a
# hallucinated label can't leak into the verdict.
_KNOWN_PATHOLOGIES = frozenset(
    {
        "reward_hacking",
        "catastrophic_forgetting",
        "under_exploration",
        "constraint_stripping",
    }
)

# Per-document char budget sent to the judge. SkillValidator caps a MINOR
# proposal at 300 lines, but current_content may be a larger legacy skill, so
# cap defensively (head+tail with an elision marker). The REMOVED-lines
# highlight is computed from the FULL documents before this cap, so a large
# constraint-strip can never hide in an elided tail.
_DOC_CHAR_BUDGET = 8000
_REMOVED_CHAR_BUDGET = 6000


def _cap(text: str, limit: int = _DOC_CHAR_BUDGET) -> str:
    """Head+tail slice with an elision marker when over budget."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    elided = len(text) - head - tail
    return f"{text[:head]}\n...[{elided} chars elided]...\n{text[-tail:]}"


def _removed_lines(current_content: str, proposed_content: str) -> str:
    """Lines the edit REMOVED (present in current, absent in proposed).

    Computed from the FULL documents (before any doc cap) so the deletion
    signal — the primary constraint-strip evidence — is never truncated away.
    """
    diff = difflib.unified_diff(
        current_content.splitlines(),
        proposed_content.splitlines(),
        lineterm="",
        n=0,
    )
    removed = [line[1:] for line in diff if line.startswith("-") and not line.startswith("---")]
    return "\n".join(removed)


def _parse_pathologies(raw_response: str) -> list[str]:
    """Best-effort extraction of the rubric's ``pathologies`` list.

    ``raw_response`` is the judge's raw output (fences possible, truncated to
    1000 chars by the scorer). Any parse failure yields an empty list — the
    pathology labels are enrichment, not the authoritative pass/fail signal.
    """
    try:
        from genesis.eval.scorers import _extract_json

        data = json.loads(_extract_json(raw_response))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    found = data.get("pathologies", [])
    if not isinstance(found, list):
        return []
    return [p for p in found if p in _KNOWN_PATHOLOGIES]


async def run_critic(
    *,
    current_content: str | None,
    proposal: SkillProposal,
    router: Router | None,
) -> dict | None:
    """Screen a skill edit for self-modification pathologies (shadow).

    Returns a verdict dict to be logged, or ``None`` when there is nothing to
    log (gate off, no router, or no baseline content). NEVER raises.

    Verdict shape::

        {"verdict": "clean" | "flagged" | "unavailable",
         "score": float,               # omitted when unavailable
         "rationale": str,              # omitted when unavailable
         "pathologies": [str],          # omitted when unavailable
         "change_size": str,
         "rubric_version": str,
         "error": str}                  # only when unavailable

    ``flagged`` = the judge scored the edit below the rubric threshold (likely
    pathological). ``unavailable`` = the judge could not be reached/parsed (a
    degrade-to-NULL record, never a false ``flagged``).
    """
    # Hard kill first, then the live settings mode.
    if skill_gate_off() or skill_gate_mode() == "off":
        return None
    # No router (degraded init) or no baseline to diff against — nothing to
    # screen. Degrade silently; the edit still applies upstream.
    if router is None or not current_content:
        return None

    removed = _removed_lines(current_content, proposal.proposed_content)
    change_size = proposal.change_size.value

    try:
        from genesis.eval.scorers import LLMJudgeScorer

        scorer = LLMJudgeScorer(router=router)
        passed, score, detail = await scorer.score_async(
            actual=_cap(proposal.proposed_content),
            expected=_cap(current_content),
            config={
                "rubric_name": _RUBRIC_NAME,
                "removed_content": removed[:_REMOVED_CHAR_BUDGET] or "(no lines removed)",
                "change_size": change_size,
                "edit_rationale": (proposal.rationale or "")[:1000],
            },
        )
    except Exception:
        # Rubric-not-registered / missing-placeholder / no-router / infra —
        # none of LLMJudgeScorer's built-in degrade paths. Record NULL.
        logger.warning(
            "skill-edit Critic scorer raised for %s — recording unavailable verdict",
            proposal.skill_name,
            exc_info=True,
        )
        return {
            "verdict": "unavailable",
            "change_size": change_size,
            "rubric_version": _RUBRIC_VERSION,
            "error": "scorer_exception",
        }

    # OUTAGE GUARD (mirrors surplus.quality_judge): score_async signals judge
    # call/parse failures by returning passed=False with an ``"error"`` key in
    # the detail JSON. Match on key PRESENCE (not sentinel strings) so new error
    # kinds stay covered — otherwise a provider outage would score every edit
    # "flagged", a false-positive flood.
    try:
        parsed = json.loads(detail) if detail else {}
    except (json.JSONDecodeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if "error" in parsed:
        logger.info(
            "skill-edit Critic unavailable (%s) for %s — recording NULL verdict",
            parsed.get("error"),
            proposal.skill_name,
        )
        return {
            "verdict": "unavailable",
            "change_size": change_size,
            "rubric_version": parsed.get("rubric_version", _RUBRIC_VERSION),
            "error": parsed.get("error"),
        }

    return {
        "verdict": "flagged" if not passed else "clean",
        "score": score,
        "rationale": parsed.get("rationale", ""),
        "pathologies": _parse_pathologies(parsed.get("raw_response", "")),
        "change_size": change_size,
        "rubric_version": parsed.get("rubric_version", _RUBRIC_VERSION),
    }
