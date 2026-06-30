"""Procedure Judge — validates candidates from Stream 1 (struggle) and Stream 2 (extraction).

Import discipline: this module is imported from memory/procedure_extraction.py
(memory → learning direction). All memory/ imports must stay deferred inside
function bodies to avoid a circular import cycle (memory ↔ learning).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

logger = logging.getLogger(__name__)

_CALL_SITE = "38_procedure_extraction"

# Timeout for Judge LLM calls. Shared by both Stream 1 and Stream 2
# callers (extraction_job.py and procedure_extraction.py).
JUDGE_TIMEOUT_SECS = 60.0

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# ── Prompts ──────────────────────────────────────────────────────────────────

# A procedure is a concrete, replayable PLAYBOOK — `steps` carries the real
# commands/paths/flags, `principle` is the specific problem solved (NOT a
# "what this teaches" essay), `scenario` is the exact replay trigger.
_JSON_SCHEMA_EXAMPLE = """\
```json
{
  "worth_storing": true,
  "reason": "why this is a concrete replayable playbook (or why not)",
  "task_type": "descriptive-kebab-slug",
  "scenario": "When <exact trigger condition for replaying this>",
  "principle": "<the specific hard problem this solves, one line>",
  "steps": ["<concrete step with the REAL command / path / flag used>", "..."],
  "tools_used": ["tool1", "tool2"],
  "context_tags": ["domain1", "domain2"],
  "tool_trigger": ["Bash", "Read"]
}
```"""

# Shared definition + skip criteria. A procedure is a step-by-step PLAYBOOK for a
# specific hard-to-replicate scenario — NOT a directive, best-practice, pattern,
# generic workflow, one-off, or essay. (Validated 2026-06-30 builder spike: this
# framing yields grounded playbooks instead of "what this teaches" summaries.)
_PROCEDURE_DEF = (
    "A PROCEDURE is a specific, step-by-step PLAYBOOK for a specific, hard-to-replicate "
    'scenario — concrete "do exactly this, then this" actions WITH THE REAL '
    "commands / paths / flags used, capturing how a particular challenge was solved so it "
    "can be REPLAYED the next time the SAME scenario occurs.\n"
    "Write `steps` using ONLY commands/paths/values that actually appear in the material "
    "below — never invent commands. `principle` is the specific problem solved (one line), "
    "NOT a 'what this teaches' summary.\n\n"
    "Set worth_storing=false (with a reason) if it is instead: a behavioral DIRECTIVE / "
    "working habit (confidence, due diligence, planning, when to ask); a generic "
    "BEST-PRACTICE or reminder; an engineering PATTERN or dev-TECHNIQUE (e.g. TDD, "
    "decouple-for-testing); a generic dev/debug/deploy/audit WORKFLOW; a ONE-OFF "
    "non-recurring event; or there is no concrete solution to replay. Those belong in a "
    "skill, the knowledge base, or CLAUDE.md — not the procedure store.\n\n"
)


def _build_struggle_prompt(spine_text: str, score: float) -> str:
    """Build struggle judge prompt. Uses concatenation instead of str.format()
    to avoid KeyError on { } in transcript content (JSON tool args, etc.)."""
    return (
        "Reconstruct a reusable PROCEDURE from this session's action spine "
        "(tool calls with their arguments + outcomes).\n\n"
        + _PROCEDURE_DEF
        + "## Action Spine\n"
        + spine_text + "\n\n"
        "## Struggle Score: " + f"{score:.2f}" + "\n\n"
        "Return JSON in backticks:\n\n"
        + _JSON_SCHEMA_EXAMPLE
    )


def _build_extraction_prompt(
    content: str, scenario: str, entities: str, chunk_context: str,
) -> str:
    """Build extraction-flag judge prompt. Uses concatenation to avoid
    KeyError on { } in transcript content."""
    return (
        "A procedure candidate was flagged during transcript extraction. "
        "Reconstruct it as a concrete playbook from the surrounding context.\n\n"
        + _PROCEDURE_DEF
        + "## Candidate\n"
        "Content: " + content + "\n"
        "Scenario: " + scenario + "\n"
        "Entities: " + entities + "\n\n"
        "## Surrounding Context\n"
        + chunk_context[:3000] + "\n\n"
        "Return JSON in backticks:\n\n"
        + _JSON_SCHEMA_EXAMPLE
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_judge_response(text: str) -> dict | None:
    """Extract JSON from Judge LLM response. Returns None on parse failure."""
    match = _JSON_BLOCK_RE.search(text)
    if match:
        text = match.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Judge returned unparseable response")
        return None

    if not isinstance(data, dict):
        return None
    if not data.get("worth_storing"):
        logger.info("Judge rejected candidate: %s", data.get("reason", "no reason"))
        return None

    # Validate required fields
    for field in ("task_type", "principle", "steps"):
        if not data.get(field):
            logger.warning("Judge response missing required field: %s", field)
            return None

    return data


_EMBEDDING_PROVIDER = None

# Fail-open rate limiter: at most one procedure per task_type per cooldown
# when the embedder is unavailable. Prevents table flooding during outages.
_FAIL_OPEN_COOLDOWN_SECS = 3600  # 1 hour
_fail_open_timestamps: dict[str, float] = {}


def _get_embedder():
    """Lazy singleton embedder for novelty check. Returns None if unavailable."""
    global _EMBEDDING_PROVIDER
    if _EMBEDDING_PROVIDER is None:
        try:
            from genesis.memory.embeddings import EmbeddingProvider

            _EMBEDDING_PROVIDER = EmbeddingProvider()
        except Exception:
            logger.warning("EmbeddingProvider unavailable for Judge novelty check")
            return None
    return _EMBEDDING_PROVIDER


async def _store_judged_procedure(
    db: aiosqlite.Connection,
    data: dict,
    router: object,
    *,
    source_type: str,
    source_session_id: str | None = None,
) -> str | None:
    """Run the scoping + novelty checks, then store via store_procedure_checked.

    Returns procedure ID on success, None on skip/duplicate/directive.
    """
    from genesis.learning.procedural.extractor import _principle_is_novel
    from genesis.learning.procedural.operations import store_procedure_checked
    from genesis.learning.procedural.scoping import is_behavioral_directive

    task_type = data["task_type"]
    principle = data["principle"]
    steps = data.get("steps", [])
    tools_used = data.get("tools_used", [])
    context_tags = data.get("context_tags", [])
    scenario = data.get("scenario")
    tool_trigger = data.get("tool_trigger")

    # Ensure steps/tools_used/context_tags are lists
    if isinstance(steps, str):
        steps = [steps]
    if isinstance(tools_used, str):
        tools_used = [tools_used]
    if isinstance(context_tags, str):
        context_tags = [context_tags]

    # Scoping gate: behavioral DIRECTIVES (general working-style rules — confidence,
    # due diligence, planning cadence) belong in CLAUDE.md, not the procedure store,
    # and are the dominant near-duplicate source. Fails open to "keep" on any
    # classifier error — never suppress a real procedure.
    if await is_behavioral_directive(
        router, task_type=task_type, principle=principle, steps=steps,
    ):
        return None

    # Cosine novelty check
    embedder = _get_embedder()
    is_novel, max_sim, principle_vec, fell_open = await _principle_is_novel(
        db, task_type=task_type, new_principle=principle, embedder=embedder,
    )

    # Fail-open rate limiter: when the novelty gate couldn't check
    # (embedder down), allow at most one per task_type per cooldown window.
    if fell_open:
        import time

        last = _fail_open_timestamps.get(task_type, 0.0)
        if time.monotonic() - last < _FAIL_OPEN_COOLDOWN_SECS:
            logger.info(
                "Judge: rate-limited fail-open store for %s (cooldown active)",
                task_type,
            )
            return None
        _fail_open_timestamps[task_type] = time.monotonic()

    if not is_novel:
        logger.info(
            "Judge: procedure for %s rejected by novelty gate (sim=%.3f)",
            task_type, max_sim,
        )
        return None

    # Pack embedding if available
    principle_blob = None
    if principle_vec is not None:
        try:
            from genesis.learning.procedural.embedding import pack_embedding

            principle_blob = pack_embedding(principle_vec)
        except Exception:
            logger.warning("Failed to pack principle embedding", exc_info=True)

    # Store via checked path (handles task_type dedup, upsert, explicit-teach guard)
    source = {"type": source_type}
    if source_session_id:
        source["session_id"] = source_session_id

    result = await store_procedure_checked(
        db,
        task_type=task_type,
        principle=principle,
        scenario=scenario,
        steps=steps,
        tools_used=tools_used,
        context_tags=context_tags,
        tool_trigger=tool_trigger,
        activation_tier="DORMANT",
        draft=1,
        success_count=0,
        confidence=0.0,
        source=source,
        principle_embedding=principle_blob,
    )

    logger.info(
        "Judge: procedure %s %s for task_type=%s (source=%s)",
        result.procedure_id, result.action, task_type, source_type,
    )

    if result.action == "skipped":
        return None

    return result.procedure_id


# ── Public entry points ──────────────────────────────────────────────────────

async def judge_struggle_procedure(
    db: aiosqlite.Connection,
    spine: list[dict],
    score: float,
    transcript_path: Path,
    router,
    *,
    source_session_id: str | None = None,
) -> str | None:
    """Judge a struggle-flagged session for procedure extraction.

    Called by Stream 1 after score_struggle() >= threshold.
    Returns procedure ID on success, None on rejection.
    """
    from genesis.learning.procedural.struggle_detector import format_spine_for_judge

    spine_text = format_spine_for_judge(spine)

    prompt = _build_struggle_prompt(spine_text, score)

    try:
        result = await router.route_call(
            call_site_id=_CALL_SITE,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        logger.warning("Judge LLM call failed for struggle procedure", exc_info=True)
        return None

    if not result.success:
        logger.warning("Judge LLM call unsuccessful: %s", result.error)
        return None

    data = _parse_judge_response(result.content)
    if data is None:
        return None

    return await _store_judged_procedure(
        db, data, router,
        source_type="struggle_extraction",
        source_session_id=source_session_id,
    )


async def judge_extraction_candidate(
    db: aiosqlite.Connection,
    candidate: dict,
    chunk_context: str,
    router,
    *,
    source_session_id: str | None = None,
) -> str | None:
    """Judge a Stream 2 procedure candidate.

    Called by procedure_extraction.py for each procedure_candidate extraction.
    Returns procedure ID on success, None on rejection.
    """
    prompt = _build_extraction_prompt(
        content=candidate.get("principle", ""),
        scenario=candidate.get("scenario", ""),
        entities=", ".join(candidate.get("tools_used", [])),
        chunk_context=chunk_context,
    )

    try:
        result = await router.route_call(
            call_site_id=_CALL_SITE,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        logger.warning("Judge LLM call failed for extraction candidate", exc_info=True)
        return None

    if not result.success:
        logger.warning("Judge LLM call unsuccessful: %s", result.error)
        return None

    data = _parse_judge_response(result.content)
    if data is None:
        return None

    return await _store_judged_procedure(
        db, data, router,
        source_type="extraction_pipeline",
        source_session_id=source_session_id,
    )
