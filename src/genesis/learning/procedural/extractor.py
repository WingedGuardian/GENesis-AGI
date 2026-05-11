"""Procedure extraction from interaction outcomes.

When the triage pipeline classifies an interaction as APPROACH_FAILURE,
WORKAROUND_SUCCESS, or (on autonomous channels) SUCCESS, this module extracts
a reusable procedure and stores it. New procedures start at L4
(advisory-only) with speculative=1 until confirmed.

Novelty gate: before storing, compute the cosine similarity of the new
procedure's principle embedding against existing procedures of the same
task_type. Skip storage if max similarity >= NOVELTY_THRESHOLD to prevent
the table from filling with paraphrases of the same insight.
"""

from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING, Any, Protocol

from genesis.learning.procedural.operations import store_procedure

if TYPE_CHECKING:
    import aiosqlite

    from genesis.memory.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)

# Cosine similarity threshold above which an extracted procedure is treated
# as a duplicate of an existing same-task_type procedure. Initial value
# calibrated by hand; see follow-up to retune from similarity-score histogram
# after 30 days of extraction data.
NOVELTY_THRESHOLD = 0.85

_EMBEDDING_PROVIDER: EmbeddingProvider | None = None


def _get_embedding_provider() -> EmbeddingProvider | None:
    """Lazy module-level singleton. Returns None if no embedding backend is
    configured (extractor falls open and stores without novelty filtering).
    """
    global _EMBEDDING_PROVIDER
    if _EMBEDDING_PROVIDER is None:
        try:
            from genesis.memory.embeddings import EmbeddingProvider

            _EMBEDDING_PROVIDER = EmbeddingProvider()
        except Exception:
            logger.warning(
                "EmbeddingProvider unavailable; procedure novelty gate disabled",
                exc_info=True,
            )
            return None
    return _EMBEDDING_PROVIDER


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def _principle_is_novel(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    new_principle: str,
    embedder: EmbeddingProvider | None,
) -> tuple[bool, float]:
    """Return (is_novel, max_similarity_seen).

    Fail-open: if embedding fails or no embedder is configured, treat as
    novel so we don't silently drop extractions when the embedding stack is
    degraded.
    """
    if embedder is None:
        return True, 0.0

    try:
        from genesis.db.crud.procedural import list_by_task_type

        existing = await list_by_task_type(db, task_type)
    except Exception:
        logger.warning(
            "Procedure novelty lookup failed; allowing storage", exc_info=True,
        )
        return True, 0.0

    if not existing:
        return True, 0.0

    try:
        new_emb = await embedder.embed(new_principle)
        max_sim = 0.0
        for row in existing:
            existing_principle = (
                row.get("principle") if isinstance(row, dict) else row["principle"]
            )
            if not existing_principle:
                continue
            existing_emb = await embedder.embed(existing_principle)
            sim = _cosine_similarity(new_emb, existing_emb)
            if sim > max_sim:
                max_sim = sim
        return max_sim < NOVELTY_THRESHOLD, max_sim
    except Exception:
        logger.warning(
            "Embedding/cosine failed in novelty gate; allowing storage",
            exc_info=True,
        )
        return True, 0.0

# 38_procedure_extraction — extracts reusable procedures from interaction outcomes.
# Currently in the learning-pipeline-only path (partially wired per _call_site_meta.py).
_CALL_SITE = "38_procedure_extraction"

_PROMPT_TEMPLATE = """\
Given this interaction summary, extract a reusable procedure that could prevent
the same failure or capture the successful workaround for future use.

## Interaction
{summary_text}

## Outcome
{outcome}

## Instructions
Return a JSON object with these fields:
- "task_type": short kebab-case identifier (e.g., "youtube-content-fetch")
- "principle": one sentence explaining why this procedure exists
- "steps": array of step strings (imperative, specific, actionable)
- "tools_used": array of tool names involved (e.g., ["Bash", "WebFetch"])
- "context_tags": array of tags for matching (e.g., ["youtube", "ssl", "video"])
- "tool_trigger": array of CC tool names that should trigger this procedure, or null

Return ONLY the JSON object, no markdown fences or explanation.
"""


class _Router(Protocol):
    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any: ...


async def extract_procedure(
    db: aiosqlite.Connection,
    *,
    summary_text: str,
    outcome: str,
    router: _Router,
    embedding_provider: EmbeddingProvider | None = None,
) -> str | None:
    """Extract a procedure from an interaction summary via LLM.

    Returns the procedure ID if successful, None if extraction fails.
    All failures are logged but never raised — this is secondary to the
    main triage pipeline and must not crash it.

    Pass `embedding_provider` to override the default lazy module-level
    singleton (useful for tests).
    """
    prompt = _PROMPT_TEMPLATE.format(summary_text=summary_text, outcome=outcome)

    try:
        result = await router.route_call(
            call_site_id=_CALL_SITE,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        logger.error("Procedure extraction LLM call failed", exc_info=True)
        return None

    try:
        text = result.content.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
    except (json.JSONDecodeError, AttributeError, IndexError):
        logger.error("Procedure extraction: failed to parse LLM response: %s", result.content[:200])
        return None

    # Validate required fields
    required = ("task_type", "principle", "steps", "tools_used", "context_tags")
    if not all(k in data and data[k] for k in required):
        logger.warning("Procedure extraction: missing required fields in %s", list(data.keys()))
        return None

    # Skip if an explicit-teach procedure already covers this task_type
    try:
        from genesis.db.crud.procedural import find_by_task_type

        existing = await find_by_task_type(db, data["task_type"])
        if existing and existing.get("speculative") == 0:
            logger.info(
                "Skipped extraction for %s: explicit-teach %s exists",
                data["task_type"], existing["id"],
            )
            return None
    except Exception:
        pass  # Non-critical guard — continue with extraction if check fails

    # Novelty gate: skip if a near-duplicate principle already exists for
    # this task_type. Fail-open when embeddings are unavailable.
    embedder = embedding_provider if embedding_provider is not None else _get_embedding_provider()
    is_novel, max_sim = await _principle_is_novel(
        db,
        task_type=data["task_type"],
        new_principle=data["principle"],
        embedder=embedder,
    )
    if not is_novel:
        logger.info(
            "Skipped extraction for %s: near-duplicate principle (cosine=%.3f >= %.2f)",
            data["task_type"], max_sim, NOVELTY_THRESHOLD,
        )
        return None

    try:
        proc_id = await store_procedure(
            db,
            task_type=data["task_type"],
            principle=data["principle"],
            steps=data["steps"],
            tools_used=data["tools_used"],
            context_tags=data["context_tags"],
            tool_trigger=data.get("tool_trigger"),
            activation_tier="L4",
            speculative=1,
            source={"type": "auto_extracted", "triage_outcome": outcome},
        )
        logger.info("Extracted procedure %s: %s", proc_id, data["task_type"])
        return proc_id
    except Exception:
        logger.error("Procedure extraction: failed to store procedure", exc_info=True)
        return None
