"""Procedure Judge — builds reusable PLAYBOOKS from struggle-detected sessions.

Single entry point: ``judge_multi_procedure()`` takes a USER-BLIND action spine
(tool actions only) + a grounding haystack (the uncapped record of executed tool
inputs) and returns 0..N topic-segmented procedures. The legacy per-candidate
chunk judge was removed in C2b — the chunk path is now flag-only (a session-level
signal), and this whole-session builder is the single judge.

Import discipline: this module is imported from memory/* (memory → learning
direction). All memory/ imports stay deferred inside function bodies to avoid a
circular import cycle (memory ↔ learning).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from genesis.learning.procedural.embedding import (
    FAIL_OPEN_COOLDOWN_SECS,
    _fail_open_timestamps,
    get_embedding_provider,
)

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_CALL_SITE = "38_procedure_extraction"

# Outer guard for the whole-session builder (extraction_job caller). NOT a single
# call: the builder makes up to ~15 sequential LLM calls (1 build + 1 retry, then
# per stored procedure a scoping check + a cross-type dedup check). Realistic
# ceiling ~150s; 300s gives >2x margin while still bounding a pathological hang.
# Each individual LLM call is separately timeout-guarded by the router — this only
# catches the case where the whole build wedges.
JUDGE_TIMEOUT_SECS = 300.0

# Grounding is OBSERVABILITY-ONLY (user decision 2026-06-30): below this fraction
# of step-tokens present in the execution record, log a warning + record the
# score — but ALWAYS store. Grounding is never a drop gate. See grounding.py.
_GROUNDING_WARN_THRESHOLD = 0.25

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# ── Prompts ──────────────────────────────────────────────────────────────────

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

# Multi-procedure builder framing (C2b). The spine is USER-BLIND — only Genesis's
# own tool actions — so the builder cannot mistake the user's request for a
# procedure. Validated against the prod chain + S-tier (~/tmp/prc_c2b_prodspike.py).
_MULTI_PROCEDURE_INTRO = (
    "You are reconstructing reusable PROCEDURES from what an AI coding agent (Genesis) DID "
    "in a session. The action spine below contains ONLY Genesis's own tool actions "
    "(commands + outcomes) — NOT the user's messages. A procedure is about what GENESIS "
    "does, never about what the user said or asked.\n\n"
)

_MULTI_SEGMENTATION = (
    "A session may contain ZERO, ONE, or SEVERAL procedures. Segment BY TOPIC / GOAL:\n"
    "- Each distinct goal accomplished via a concrete replayable playbook = one procedure "
    "(e.g. \"publish a post to Medium\").\n"
    "- ALSO emit a SEPARATE procedure for a sub-sequence that is INDEPENDENTLY REUSABLE in a "
    "DIFFERENT task (set is_subprocedure_of to the parent task_type) — captured specifically "
    "(exact tool/selectors/commands). Do this ONLY when the sub-sequence would genuinely be "
    "replayed on its own in other contexts; do NOT split every step into its own procedure.\n"
    "- Do NOT fragment one continuous flow into many tiny procedures, and skip a 'procedure' "
    "that is just a single trivial command. MOST sessions contain no replayable playbook — "
    "return an empty list.\n\n"
    "ONE-OFF TEST (critical): a real procedure must be REPLAYABLE with DIFFERENT inputs the "
    "next time the scenario recurs. If the steps only make sense for THIS session's specific "
    "data — hardcoded row/record/follow-up IDs, one-time resolution notes, a specific list of "
    "items that will not recur — it is a one-off cleanup, NOT a playbook: do NOT emit it. If a "
    "candidate mixes a generic investigation with one-off actions tied to specific IDs, skip "
    "it. Ask: \"next month, in a NEW situation, could Genesis replay these exact steps?\" If "
    "no, skip.\n\n"
    "EVERY step must quote an EXACT command/path/flag that appears verbatim in the spine — "
    "never paraphrase or invent. For credentials / PII: never embed actual secrets — reference "
    "the reference store instead (e.g. \"use the medium.com login from the reference store\").\n\n"
)

_MULTI_SCHEMA_EXAMPLE = """\
```json
{"procedures": [{"task_type": "kebab-slug", "scenario": "When <trigger>", "principle": "<specific problem solved, one line>", "steps": ["<concrete step with the REAL command/path/flag>"], "tools_used": ["..."], "context_tags": ["..."], "tool_trigger": ["Bash"], "is_subprocedure_of": "<parent task_type or null>"}]}
```"""


def _build_multi_struggle_prompt(spine_text: str, score: float) -> str:
    """Build the multi-procedure builder prompt. Uses concatenation (NOT
    str.format) so { } in transcript content can't raise KeyError."""
    return (
        _MULTI_PROCEDURE_INTRO
        + _PROCEDURE_DEF
        + _MULTI_SEGMENTATION
        + "## Action Spine\n"
        + spine_text + "\n\n"
        "## Struggle Score: " + f"{score:.2f}" + "\n\n"
        "Return ONLY this JSON in backticks (the list may be empty):\n\n"
        + _MULTI_SCHEMA_EXAMPLE
    )


# ── Parsing ──────────────────────────────────────────────────────────────────

def _coerce_json(text: object) -> object | None:
    """Extract a JSON value from an LLM response (handles ```json fences).
    Returns the parsed object, or None on any parse failure."""
    if not isinstance(text, str):
        return None
    match = _JSON_BLOCK_RE.search(text)
    raw = match.group(1) if match else text
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_judge_response(text: str) -> dict | None:
    """Validate a SINGLE judged-procedure dict. Returns None on parse failure,
    worth_storing=false, or a missing required field. (Retained as the documented
    single-item contract; exercised directly by the parsing tests.)"""
    data = _coerce_json(text)
    if data is None:
        logger.warning("Judge returned unparseable response")
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("worth_storing"):
        logger.info("Judge rejected candidate: %s", data.get("reason", "no reason"))
        return None
    for field in ("task_type", "principle", "steps"):
        if not data.get(field):
            logger.warning("Judge response missing required field: %s", field)
            return None
    return data


def _parse_judge_response_list(text: str) -> list[dict] | None:
    """Parse the multi-procedure builder response.

    Returns ``None`` when the envelope is unparseable (caller may retry once),
    ``[]`` when it parses but yields no valid procedure, or the list of valid
    procedure dicts (each must carry task_type/principle/steps). Per-item
    validation is defensive — one malformed item never discards the rest.
    """
    data = _coerce_json(text)
    if data is None:
        return None  # unparseable → signal retry
    if isinstance(data, dict):
        procs = data.get("procedures")
    elif isinstance(data, list):
        procs = data
    else:
        procs = None
    if not isinstance(procs, list):
        return []
    valid: list[dict] = []
    for item in procs:
        try:
            if isinstance(item, dict) and all(
                item.get(f) for f in ("task_type", "principle", "steps")
            ):
                valid.append(item)
        except Exception:
            continue  # never let one bad item sink the batch
    return valid


# ── Storage ──────────────────────────────────────────────────────────────────

async def _store_judged_procedure(
    db: aiosqlite.Connection,
    data: dict,
    router: object,
    *,
    source_type: str,
    source_session_id: str | None = None,
    grounding_score: float | None = None,
) -> str | None:
    """Run the scoping + (same-type + cross-type) novelty checks, then store via
    store_procedure_checked. Returns procedure ID on success, None on
    skip/duplicate/directive."""
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

    # Novelty: same-task_type cosine gate + cross-task_type LLM dedup (router-gated).
    embedder = get_embedding_provider()
    is_novel, max_sim, principle_vec, fell_open = await _principle_is_novel(
        db, task_type=task_type, new_principle=principle, embedder=embedder,
        router=router, new_steps=steps,
    )

    # Fail-open rate limiter: when the novelty gate couldn't check (embedder down),
    # allow at most one per task_type per cooldown window. Shared across both
    # extraction paths via embedding._fail_open_timestamps.
    if fell_open:
        import time

        last = _fail_open_timestamps.get(task_type, 0.0)
        if time.monotonic() - last < FAIL_OPEN_COOLDOWN_SECS:
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

    # Store via checked path (handles task_type dedup, upsert, explicit-teach guard).
    # grounding_score is recorded for observability (warning-only gate); the JSON
    # source blob is queryable via json_extract(source, '$.grounding_score').
    source: dict = {"type": source_type}
    if source_session_id:
        source["session_id"] = source_session_id
    if grounding_score is not None:
        source["grounding_score"] = round(grounding_score, 3)

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


# ── Public entry point ─────────────────────────────────────────────────────────

async def _call_and_parse_list(router: object, prompt: str) -> list[dict]:
    """Call the builder LLM and parse a procedure list, retrying ONCE on an
    unparseable envelope. Returns [] on call failure or persistent unparse."""
    for attempt in (1, 2):
        try:
            result = await router.route_call(
                call_site_id=_CALL_SITE,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            logger.warning(
                "Judge LLM call failed for multi-procedure (attempt %d)", attempt,
                exc_info=True,
            )
            return []
        if not result.success:
            logger.warning(
                "Judge LLM call unsuccessful (attempt %d): %s", attempt, result.error,
            )
            return []
        parsed = _parse_judge_response_list(result.content)
        if parsed is not None:
            return parsed
        logger.warning(
            "Judge multi-procedure response unparseable (attempt %d of 2)", attempt,
        )
    return []


async def judge_multi_procedure(
    db: aiosqlite.Connection,
    spine: list[dict],
    haystack: str,
    score: float,
    router,
    *,
    source_session_id: str | None = None,
    max_new: int | None = None,
) -> list[str]:
    """Build 0..N topic-segmented procedures from a struggle-detected session.

    ``spine`` is rendered USER-BLIND (tool actions only); ``haystack`` is the
    uncapped execution record used for grounding. Each built procedure is grounded
    (warning-only — never dropped), scoping-gated, novelty-checked, and stored.
    Returns the list of stored procedure IDs (capped at ``max_new`` when given).
    """
    from genesis.learning.procedural.grounding import grounding_score
    from genesis.learning.procedural.struggle_detector import format_spine_for_judge

    spine_text = format_spine_for_judge(spine)
    prompt = _build_multi_struggle_prompt(spine_text, score)

    procedures = await _call_and_parse_list(router, prompt)
    if not procedures:
        return []

    stored: list[str] = []
    for data in procedures:
        if max_new is not None and len(stored) >= max_new:
            logger.info(
                "Multi-procedure builder hit per-session cap (%d); %d more not stored",
                max_new, len(procedures) - len(stored),
            )
            break

        steps = data.get("steps", [])
        if isinstance(steps, str):
            steps = [steps]

        # Grounding: WARNING-ONLY observability. Low score → log + record, store anyway.
        try:
            gscore = grounding_score(steps, haystack)
        except Exception:
            gscore = 1.0
        if gscore < _GROUNDING_WARN_THRESHOLD:
            logger.warning(
                "Procedure '%s' weakly grounded (%.0f%% of step tokens in the "
                "execution record) — storing anyway (grounding is observability only)",
                data.get("task_type"), gscore * 100,
            )

        # Sub-procedure linkage → context_tags tag (no schema change).
        parent = data.get("is_subprocedure_of")
        if parent and str(parent).strip().lower() not in ("", "null", "none"):
            tags = data.get("context_tags") or []
            if isinstance(tags, str):
                tags = [tags]
            data["context_tags"] = [*tags, f"subprocedure_of:{parent}"]

        proc_id = await _store_judged_procedure(
            db, data, router,
            source_type="struggle_extraction",
            source_session_id=source_session_id,
            grounding_score=gscore,
        )
        if proc_id:
            stored.append(proc_id)

    return stored
