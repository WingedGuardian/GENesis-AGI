"""Procedure extraction from interaction outcomes.

When the triage pipeline classifies an interaction as APPROACH_FAILURE or
WORKAROUND_SUCCESS, this module extracts a reusable procedure and stores it.
New procedures start at L4 (advisory-only) with speculative=1 until confirmed.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

from genesis.learning.procedural.operations import store_procedure

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

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
) -> str | None:
    """Extract a procedure from an interaction summary via LLM.

    Returns the procedure ID if successful, None if extraction fails.
    All failures are logged but never raised — this is secondary to the
    main triage pipeline and must not crash it.
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
