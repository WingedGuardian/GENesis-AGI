"""MW-2 lean keystone — the reusable relationship-classifier function.

Given two memory contents, classify their relationship into a COARSE, tractable
vocabulary and attach a confidence. This is the judgment MW-5's merge gate
consumes (``≥0.95`` cosine only NOMINATES; this function confirms same-assertion
and rules out old-vs-new-truth / contradiction before any merge).

Design decisions (see plan ``yes-go-ahead-as-humming-gosling.md`` MW-2 LEAN):

- **Coarse vocabulary only** — ``{duplicate, contradicts, succeeded_by,
  distinct}``. Generalizes ``entity_resolution.check_semantic_overlap``
  (duplicate/contradicts/distinct) by adding ``succeeded_by`` (old→new truth).
  Fine-grained typing (extends/supports/elaborates) is DELIBERATELY not
  attempted: it is literature-unreliable on already-similar pairs, and NO
  consumer reads a fabricated fine type (recall boost is type-agnostic —
  ``neighbors_of`` ranks by ``MAX(strength)``, only the ``contradicts`` deny-list
  uses type). Attempting it would replace a harmless heuristic with an
  expensive, unreliable one.
- **Direction-agnostic verdict** — ``succeeded_by`` means "one supersedes the
  other"; the CALLER orders older→newer from ``created_at`` (as
  ``dream_entity_scan`` already does). An optional ``newer`` hint is passed to
  the prompt to help the model separate a temporal UPDATE (succeeded_by) from a
  genuine CONFLICT (contradicts).
- **Fail-safe** — any LLM/parse failure, an out-of-vocab verdict, or an
  absent/low confidence resolves to ``distinct`` at confidence ``0.0``. Never
  fabricate a merge-eligible verdict on uncertainty (mirrors the precedent's
  ``distinct`` default; a wrong ``duplicate`` would drive a wrong merge).

NOTHING mutates the graph here — this is a pure judgment function. The MW-2 lean
keystone ships it + a shadow measurement probe; the stored-graph reclassification
/ boost-gating machinery (MW-2b) is deferred behind that measurement.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

#: Observability call site — registered in ``observability/_call_site_meta.py``
#: with ``model_tier="slm"`` (same tier as ``dream_cycle_entity_check``).
CALL_SITE_ID: str = "dream_cycle_relationship_classify"

#: The ONLY relationship labels this classifier may emit. Anything else the LLM
#: returns is coerced to ``distinct`` (fail-safe). Deliberately excludes the
#: fine-grained types (extends/supports/elaborates/related_to) — see module doc.
COARSE_RELATIONSHIPS: frozenset[str] = frozenset(
    {"duplicate", "contradicts", "succeeded_by", "distinct"}
)

#: Truncation applied to each memory's content before prompting (mirrors
#: ``check_semantic_overlap``'s 1500-char cap — keeps token cost bounded).
_CONTENT_CAP = 1500

_DEFINITIONS = """\
- "duplicate": the SAME assertion/information, possibly reworded.
- "contradicts": the same topic, but CONFLICTING claims (different numbers, opposite conclusions).
- "succeeded_by": the same topic, where one memory UPDATES/supersedes the other (newer truth replaces older).
- "distinct": a related topic but genuinely DIFFERENT, non-conflicting information.

confidence = how sure you are, 0.0-1.0. Use LOW confidence when the pair is only loosely related or the distinction is genuinely ambiguous. Do NOT guess "duplicate" or "contradicts" when unsure — prefer "distinct" with low confidence."""

_EXAMPLES = """\
Examples:
A: "The deploy host runs the server" / B: "The deploy host runs the server (reworded)" -> {"relationship":"duplicate","confidence":0.95,"reasoning":"same fact reworded"}
A: "Budget is 100/day" / B: "Budget is 250/day" -> {"relationship":"contradicts","confidence":0.9,"reasoning":"conflicting numbers, same subject"}
A: "We chose engine X" / B: "We switched from X to engine Y" -> {"relationship":"succeeded_by","confidence":0.85,"reasoning":"B updates the decision in A"}
A: "Notes about the voice pipeline" / B: "Notes about the billing flow" -> {"relationship":"distinct","confidence":0.8,"reasoning":"different subjects"}"""

_SINGLE_PROMPT = """\
Compare Memory A and Memory B and decide their relationship. Respond with JSON only, no other text:
{{"relationship": "duplicate|contradicts|succeeded_by|distinct", "confidence": 0.0-1.0, "reasoning": "one sentence"}}

{definitions}

{examples}
{newer_hint}
Memory A:
{content_a}

Memory B:
{content_b}"""

_BATCH_PROMPT = """\
Compare each numbered pair of memories and decide the relationship for EACH. Respond with a JSON ARRAY only, one object per pair, in order:
[{{"pair_id": 0, "relationship": "duplicate|contradicts|succeeded_by|distinct", "confidence": 0.0-1.0, "reasoning": "one sentence"}}, ...]

{definitions}

{examples}

Pairs:
{pairs}"""


def _newer_hint(newer: str | None) -> str:
    if newer == "a":
        return "\nNOTE: Memory A is the NEWER memory (if one updates the other, A is the update).\n"
    if newer == "b":
        return "\nNOTE: Memory B is the NEWER memory (if one updates the other, B is the update).\n"
    return "\n"


def _failsafe(reasoning: str = "classifier fail-safe → distinct") -> dict[str, Any]:
    return {"relationship": "distinct", "confidence": 0.0, "reasoning": reasoning}


def _parse_json(text: str | None) -> Any:
    """Strip an optional markdown fence and ``json.loads``; ``None`` on failure."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        # ```json\n...\n``` → drop the opening fence line and the closing fence.
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None


def _normalize_verdict(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw parsed object into a trusted verdict.

    Out-of-vocab relationship OR non-numeric/absent confidence ⇒ the value is not
    trusted: an out-of-vocab type becomes ``distinct``; an absent confidence
    becomes ``0.0`` (never silently high). A valid label keeps its clamped
    confidence.
    """
    rel = raw.get("relationship")
    reasoning = str(raw.get("reasoning", "") or "")
    if rel not in COARSE_RELATIONSHIPS:
        return {"relationship": "distinct", "confidence": 0.0, "reasoning": reasoning}
    conf = raw.get("confidence")
    # bool is a subclass of int — reject it explicitly so True→1.0 can't sneak in.
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        conf = 0.0
    conf = max(0.0, min(1.0, float(conf)))
    return {"relationship": rel, "confidence": conf, "reasoning": reasoning}


async def classify_relationship(
    router: Router,
    content_a: str,
    content_b: str,
    *,
    newer: str | None = None,
) -> dict[str, Any]:
    """Classify the relationship between two memory contents.

    Returns ``{"relationship", "confidence", "reasoning"}`` where ``relationship``
    is one of :data:`COARSE_RELATIONSHIPS`. Fail-safe = ``distinct`` @ ``0.0``.

    ``newer`` (``"a"``/``"b"``/``None``) tells the model which memory is newer, to
    separate a temporal update (``succeeded_by``) from a conflict
    (``contradicts``). Direction of ``succeeded_by`` is the caller's to apply.
    """
    prompt = _SINGLE_PROMPT.format(
        definitions=_DEFINITIONS,
        examples=_EXAMPLES,
        newer_hint=_newer_hint(newer),
        content_a=content_a[:_CONTENT_CAP],
        content_b=content_b[:_CONTENT_CAP],
    )
    try:
        result = await router.route_call(
            CALL_SITE_ID,
            [{"role": "user", "content": prompt}],
            suppress_dead_letter=True,
        )
        if not result.success:
            logger.warning(
                "relationship classify LLM call failed: %s", getattr(result, "error", None)
            )
            return _failsafe(f"LLM error: {getattr(result, 'error', None)}")
        data = _parse_json(result.content)
        if not isinstance(data, dict):
            return _failsafe("unparseable verdict → distinct")
        return _normalize_verdict(data)
    except Exception:  # noqa: BLE001 — a classifier failure must never propagate
        logger.debug("relationship classify parse/call error", exc_info=True)
        return _failsafe("exception → distinct")


def _render_pairs(pairs: Sequence[tuple[str, str]]) -> str:
    blocks = []
    for i, (a, b) in enumerate(pairs):
        blocks.append(f"[{i}]\nMemory A: {a[:_CONTENT_CAP]}\nMemory B: {b[:_CONTENT_CAP]}")
    return "\n\n".join(blocks)


async def classify_relationships(
    router: Router,
    pairs: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Batched :func:`classify_relationship` — one LLM call for many pairs.

    Returns a verdict list aligned to ``pairs`` by index. Any pair the model omits
    or malforms fails safe to ``distinct`` @ ``0.0``; a whole-call failure fails
    every pair safe. Empty ``pairs`` returns ``[]`` with NO LLM call.
    """
    if not pairs:
        return []
    prompt = _BATCH_PROMPT.format(
        definitions=_DEFINITIONS,
        examples=_EXAMPLES,
        pairs=_render_pairs(pairs),
    )
    try:
        result = await router.route_call(
            CALL_SITE_ID,
            [{"role": "user", "content": prompt}],
            suppress_dead_letter=True,
        )
        if not result.success:
            logger.warning(
                "relationship classify (batch) failed: %s", getattr(result, "error", None)
            )
            return [_failsafe(f"LLM error: {getattr(result, 'error', None)}") for _ in pairs]
        data = _parse_json(result.content)
        if not isinstance(data, list):
            return [_failsafe("unparseable batch → distinct") for _ in pairs]
        by_id: dict[int, dict[str, Any]] = {}
        for item in data:
            if (
                isinstance(item, dict)
                and isinstance(item.get("pair_id"), int)
                and not isinstance(item.get("pair_id"), bool)
            ):
                by_id[item["pair_id"]] = item
        return [
            _normalize_verdict(by_id[i]) if i in by_id else _failsafe("missing in batch → distinct")
            for i in range(len(pairs))
        ]
    except Exception:  # noqa: BLE001 — a classifier failure must never propagate
        logger.debug("relationship classify (batch) error", exc_info=True)
        return [_failsafe("exception → distinct") for _ in pairs]
