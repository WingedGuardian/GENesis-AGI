"""Adversarial review for dream cycle synthesis and entity resolution.

Uses a different LLM provider than the synthesizer to verify faithfulness.
Synthesis: DeepSeek produces, Kimi challenges.
Entity: Kimi judges, DeepSeek challenges.

Fail-safe: any error or ambiguity defaults to blocking deprecation.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

CALL_SITE_SYNTHESIS = "dream_cycle_synthesis_challenge"
CALL_SITE_ENTITY = "dream_cycle_entity_challenge"

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_SYNTHESIS_REVIEW_PROMPT = """\
You are a memory quality adversary. Your job is to FIND PROBLEMS, not confirm quality.

ORIGINAL MEMORIES ({n} total):
{originals}

PROPOSED SYNTHESIS:
{synthesis}

List every distinct fact, date, entity, decision, or relationship present in ANY \
original that is missing, distorted, or weakened in the synthesis.

Respond with JSON only, no other text:
- If you find information loss: {{"verdict": "FAIL", "missing": ["<specific item 1>", "<specific item 2>"]}}
- If the synthesis faithfully preserves all information: {{"verdict": "PASS"}}

Also check: do any of the originals contradict each other due to being recorded \
at different times? If the synthesis resolves a temporal conflict, does it preserve \
the CURRENT state and note the historical change? Flag any case where outdated \
information is presented as current in the missing list.

Your default assumption is that something was lost. You must actively convince \
yourself nothing is missing before returning PASS."""

_ENTITY_REVIEW_PROMPT = """\
Compare these two memories. A previous reviewer called them "duplicate" and wants \
to merge them (deprecating one). Your job is to challenge that verdict.

Memory A:
{content_a}

Memory B:
{content_b}

Are these truly duplicates (same information, just reworded)? Or do they record \
genuinely different facts, events, or contexts?

Respond with JSON only, no other text:
{{"relationship": "duplicate|distinct", "reasoning": "one sentence"}}

Lean toward "distinct" — it is safer to keep both than to lose information."""


class SynthesisBlockedError(Exception):
    """Raised when adversarial review blocks a synthesis."""

    def __init__(self, missing: list[str] | None = None, error: str | None = None):
        self.missing = missing or []
        self.error = error
        detail = ", ".join(self.missing) if self.missing else (error or "unknown")
        super().__init__(f"Synthesis blocked by adversarial review: {detail}")


@dataclass(frozen=True)
class AdversarialVerdict:
    """Result of an adversarial review."""
    passed: bool
    missing: list[str] = field(default_factory=list)
    raw_response: str = ""
    error: str | None = None


def _parse_verdict(raw: str) -> AdversarialVerdict:
    """Parse adversarial review response. Defaults to FAIL on any error."""
    text = raw.strip()
    # Strip markdown fence if present
    match = _JSON_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return AdversarialVerdict(
            passed=False, raw_response=raw,
            error="JSON parse error — defaulting to FAIL",
        )

    verdict = data.get("verdict", "").upper()
    if verdict == "PASS":
        return AdversarialVerdict(passed=True, raw_response=raw)
    else:
        return AdversarialVerdict(
            passed=False,
            missing=data.get("missing", []),
            raw_response=raw,
        )


async def check_synthesis_faithfulness(
    *,
    router: Router,
    originals: list[dict[str, Any]],
    synthesis_text: str,
) -> AdversarialVerdict:
    """Ask an adversarial LLM whether a synthesis faithfully covers all originals.

    Returns AdversarialVerdict. On any error, defaults to FAIL (fail-safe).
    """
    original_blocks = []
    for i, mem in enumerate(originals, 1):
        content = mem.get("content", "")
        confidence = mem.get("confidence", "?")
        original_blocks.append(f"--- Original {i} (confidence {confidence}):\n{content}")

    prompt = _SYNTHESIS_REVIEW_PROMPT.format(
        n=len(originals),
        originals="\n\n".join(original_blocks),
        synthesis=synthesis_text,
    )

    try:
        result = await router.route_call(
            CALL_SITE_SYNTHESIS,
            [{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("Adversarial review call failed: %s", exc)
        return AdversarialVerdict(
            passed=False, error=f"router error: {exc}",
        )

    if not result.success:
        logger.warning("Adversarial review LLM call failed: %s", result.error)
        return AdversarialVerdict(
            passed=False, error=f"LLM error: {result.error}",
        )

    return _parse_verdict(result.content or "")


async def check_entity_duplicate(
    *,
    router: Router,
    content_a: str,
    content_b: str,
) -> dict[str, str]:
    """Get adversarial second opinion on an entity 'duplicate' verdict.

    Returns {"relationship": "duplicate"|"distinct", "reasoning": "..."}.
    Defaults to "distinct" on any error (fail-safe — preserve both).
    """
    prompt = _ENTITY_REVIEW_PROMPT.format(
        content_a=content_a[:1500],
        content_b=content_b[:1500],
    )

    try:
        result = await router.route_call(
            CALL_SITE_ENTITY,
            [{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("Entity adversarial review failed: %s", exc)
        return {"relationship": "distinct", "reasoning": f"error: {exc}"}

    if not result.success:
        return {"relationship": "distinct", "reasoning": f"LLM error: {result.error}"}

    text = (result.content or "").strip()
    match = _JSON_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()

    try:
        data = json.loads(text)
        rel = data.get("relationship", "distinct")
        if rel not in ("duplicate", "distinct"):
            rel = "distinct"
        return {"relationship": rel, "reasoning": data.get("reasoning", "")}
    except (json.JSONDecodeError, Exception):
        return {"relationship": "distinct", "reasoning": "parse error — defaulting to distinct"}
