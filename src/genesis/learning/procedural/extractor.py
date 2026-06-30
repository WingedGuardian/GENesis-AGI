"""Procedure extraction from interaction outcomes.

When the triage pipeline classifies an interaction as APPROACH_FAILURE,
WORKAROUND_SUCCESS, or (on autonomous channels) SUCCESS, this module extracts
a reusable procedure and stores it. New procedures start at LIBRARY with
draft=1 and confidence=0.5 — immediately recallable and eligible for
proactive surfacing (but not blind session-start injection, which is CORE-only).

Quality gate: the extraction prompt includes criteria for the LLM to
self-assess whether the procedure codifies correct behavior and is
genuinely reusable.  A ``skip`` flag or low ``reusability_score`` aborts.

Novelty gate: cosine similarity of the new procedure's principle embedding
against existing procedures of the same task_type.  Skip storage if max
similarity >= NOVELTY_THRESHOLD to prevent paraphrased duplicates.

Cross-type contradiction check: after the same-type novelty gate passes,
check for trusted procedures with overlapping context_tags across all
task_types.  Blocks cross-type duplicates and warns on contradictions.

Fail-open rate limiter: when embedding is unavailable, a per-task-type
cooldown prevents flooding the table with unchecked near-duplicates.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Protocol

from genesis.learning.procedural.embedding import (
    FAIL_OPEN_COOLDOWN_SECS,
    _fail_open_timestamps,
    cosine_similarity,
    get_embedding_provider,
    unpack_embedding,
)
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

# ── Cross-type novelty (LLM dedup) ─────────────────────────────────────────
# The same-task_type cosine gate (NOVELTY_THRESHOLD) cannot catch a paraphrase
# stored under a DIFFERENT slug. After the same-type gate passes, scan ALL active
# procedures by stored-embedding cosine, prefilter the nearest, and ask ONE LLM
# "is the new procedure redundant with any of these?". Precision-first (the dedup
# spike found 0 false-merges on S-tier); fail-open (any error → treat as novel).
_NOVELTY_CALL_SITE = "38a_procedure_novelty_llm"
CROSS_TYPE_PREFILTER = 0.62   # cosine floor for candidates (spike found dups @0.66)
CROSS_TYPE_TOPK = 10          # cap candidates sent to the LLM (bounds cost)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_CROSS_TYPE_DEDUP_PROMPT = """\
You are deduplicating an AI agent's PROCEDURE store. A NEW procedure was just built. Decide whether it is REDUNDANT with any EXISTING procedure listed below.

REDUNDANT = the same goal achieved with essentially the same commands; OR the new one is a paraphrase of an existing one; OR an existing one FULLY CONTAINS the new one (a superset) so the new one adds nothing on its own.
DISTINCT (keep the new one) = a different goal, different tools/commands, OR each is independently useful in its own distinct situation (even within the same domain). A genuinely reusable sub-step that stands on its own is DISTINCT from a parent that contains it. When unsure, answer DISTINCT.

NEW procedure:
  task_type: {nt}
  principle: {np}
  steps: {ns}

EXISTING procedures:
{candidates}

Return ONLY this JSON in backticks:
```json
{{"redundant_with": <number of the existing procedure it duplicates, or null>, "reason": "<one line>"}}
```"""


def _row_get(row: object, key: str) -> object:
    return row.get(key) if isinstance(row, dict) else row[key]


async def _cross_type_duplicate(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    principle: str,
    new_steps: list[str] | None,
    new_emb: list[float] | None,
    router: object | None,
) -> tuple[bool, float]:
    """Best-effort cross-task_type dedup. Returns (is_duplicate, max_cross_sim).

    Fail-open: no router, no embedding, or any error → (False, …) so storage is
    never blocked by a dedup failure. Precision over recall — when unsure, store.
    """
    if router is None or new_emb is None:
        return False, 0.0
    try:
        from genesis.db.crud.procedural import list_active

        active = await list_active(db, limit=500)
    except Exception:
        logger.warning(
            "Cross-type dedup: list_active failed; treating as novel", exc_info=True,
        )
        return False, 0.0

    scored: list[tuple[float, object]] = []
    for row in active:
        if _row_get(row, "task_type") == task_type:
            continue  # same-type already handled by the cosine gate
        vec = unpack_embedding(_row_get(row, "principle_embedding"))
        if vec is None:
            continue  # no stored embedding → skip (cross-type is best-effort)
        sim = cosine_similarity(new_emb, vec)
        if sim >= CROSS_TYPE_PREFILTER:
            scored.append((sim, row))
    if not scored:
        return False, 0.0
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:CROSS_TYPE_TOPK]
    max_cross_sim = top[0][0]

    lines = []
    for i, (_sim, row) in enumerate(top, 1):
        try:
            steps = json.loads(_row_get(row, "steps")) if _row_get(row, "steps") else []
        except Exception:
            steps = []
        steps_txt = " | ".join(str(s)[:160] for s in (steps or [])[:6])
        lines.append(
            f"  [{i}] task_type: {_row_get(row, 'task_type')}\n"
            f"      principle: {_row_get(row, 'principle') or ''}\n"
            f"      steps: {steps_txt}"
        )
    ns_txt = " | ".join(str(s)[:160] for s in (new_steps or [])[:6])
    prompt = _CROSS_TYPE_DEDUP_PROMPT.format(
        nt=task_type, np=principle, ns=ns_txt, candidates="\n".join(lines),
    )

    try:
        result = await router.route_call(
            call_site_id=_NOVELTY_CALL_SITE,
            messages=[{"role": "user", "content": prompt}],
        )
        if not getattr(result, "success", False):
            return False, max_cross_sim
        match = _JSON_BLOCK_RE.search(result.content or "")
        data = json.loads(match.group(1) if match else (result.content or ""))
        rw = data.get("redundant_with")
        if isinstance(rw, int) and 1 <= rw <= len(top):
            dup = top[rw - 1][1]
            logger.info(
                "Cross-type duplicate: new '%s' ~ existing '%s' (cosine=%.3f): %s",
                task_type, _row_get(dup, "task_type"), top[rw - 1][0],
                data.get("reason", ""),
            )
            return True, max_cross_sim
    except Exception:
        logger.warning("Cross-type dedup LLM failed; treating as novel", exc_info=True)
    return False, max_cross_sim


async def _principle_is_novel(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    new_principle: str,
    embedder: EmbeddingProvider | None,
    router: object | None = None,
    new_steps: list[str] | None = None,
) -> tuple[bool, float, list[float] | None, bool]:
    """Return (is_novel, max_similarity_seen, new_principle_vector, fell_open).

    Two layers: (1) same-task_type cosine gate (BLOB-first — uses each row's
    stored embedding, falling back to a live embed only when the BLOB is NULL);
    (2) when ``router`` is supplied, a cross-task_type LLM dedup that catches a
    paraphrase stored under a different slug (the same-type gate's blind spot).

    ``fell_open`` is True when the gate could not perform a real check (embedder
    unavailable, lookup failed) and defaulted to "novel". Callers use it to apply
    the fail-open rate limiter. The cross-type layer is best-effort and never
    sets ``fell_open`` (a dedup-LLM failure just stores, it isn't an outage).
    """
    if embedder is None:
        return True, 0.0, None, True

    try:
        from genesis.db.crud.procedural import list_by_task_type

        existing = await list_by_task_type(db, task_type)
    except Exception:
        logger.warning(
            "Procedure novelty lookup failed; allowing storage", exc_info=True,
        )
        return True, 0.0, None, True

    try:
        new_emb = await embedder.embed(new_principle)
    except Exception:
        logger.warning(
            "Failed to embed new principle; allowing storage without novelty check",
            exc_info=True,
        )
        return True, 0.0, None, True

    # ── Layer 1: same-task_type cosine gate (BLOB-first) ──
    max_sim = 0.0
    try:
        for row in existing:
            existing_principle = _row_get(row, "principle")
            if not existing_principle:
                continue
            existing_emb = unpack_embedding(_row_get(row, "principle_embedding"))
            if existing_emb is None:
                try:
                    existing_emb = await embedder.embed(existing_principle)
                except Exception:
                    continue  # skip this row, keep checking the rest
            sim = cosine_similarity(new_emb, existing_emb)
            if sim > max_sim:
                max_sim = sim
    except Exception:
        logger.warning(
            "Embedding/cosine failed in novelty gate; allowing storage",
            exc_info=True,
        )
        return True, max_sim, new_emb, True

    if max_sim >= NOVELTY_THRESHOLD:
        return False, max_sim, new_emb, False

    # ── Layer 2: cross-task_type LLM dedup (best-effort, fail-open) ──
    is_dup, cross_sim = await _cross_type_duplicate(
        db,
        task_type=task_type,
        principle=new_principle,
        new_steps=new_steps,
        new_emb=new_emb,
        router=router,
    )
    seen = max(max_sim, cross_sim)
    if is_dup:
        return False, seen, new_emb, False
    return True, seen, new_emb, False

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

## Quality Gate
Before returning a procedure, validate:
1. Does this codify the CORRECT approach, or a workaround for a problem that
   has a proper solution?  (e.g., "search for metadata" when the real tool
   works is a BAD procedure — it codifies giving up.)
2. Is this genuinely reusable across multiple future tasks, or is it specific
   to this one interaction?
3. Would an expert endorse this specific approach?

If any answer is "no", return {{"skip": true, "reason": "..."}} instead.

## Instructions
Return a JSON object with these fields:
- "task_type": short kebab-case identifier (e.g., "youtube-content-fetch")
- "principle": one sentence explaining why this procedure exists
- "steps": array of step strings (imperative, specific, actionable)
- "tools_used": array of tool names involved (e.g., ["Bash", "WebFetch"])
- "context_tags": array of tags for matching (e.g., ["youtube", "ssl", "video"])
- "tool_trigger": array of CC tool names that should trigger this procedure, or null
- "reusability_score": float 0.0-1.0 (how likely is this to help future tasks?)

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
    session_tools_count: int = 0,
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

    # ── Quality gate (FM3): LLM self-assessment ──────────────────────────
    # Check skip/reusability BEFORE required-fields validation — when the
    # LLM returns {"skip": true, ...} the procedure fields are absent.
    if data.get("skip"):
        logger.info(
            "Extraction quality gate: skipped — %s",
            data.get("reason", "no reason"),
        )
        return None

    reusability = data.get("reusability_score")
    if reusability is not None and isinstance(reusability, (int, float)) and reusability < 0.5:
        logger.info(
            "Extraction quality gate: low reusability score (%.2f)",
            reusability,
        )
        return None

    # Validate required fields
    required = ("task_type", "principle", "steps", "tools_used", "context_tags")
    if not all(k in data and data[k] for k in required):
        logger.warning("Procedure extraction: missing required fields in %s", list(data.keys()))
        return None

    # Scoping gate: behavioral DIRECTIVES (general working-style rules) belong in
    # CLAUDE.md, not the procedure store, and are the dominant near-duplicate source.
    # Fails open to "keep" on any classifier error — never suppress a real procedure.
    from genesis.learning.procedural.scoping import is_behavioral_directive

    if await is_behavioral_directive(
        router, task_type=data["task_type"], principle=data["principle"], steps=data["steps"],
    ):
        logger.info(
            "Extraction: skipping behavioral directive %s (belongs in CLAUDE.md)",
            data["task_type"],
        )
        return None

    # Skip if an explicit-teach procedure already covers this task_type
    try:
        from genesis.db.crud.procedural import find_by_task_type

        existing = await find_by_task_type(db, data["task_type"])
        if existing and existing.get("draft") == 0:
            logger.info(
                "Skipped extraction for %s: explicit-teach %s exists",
                data["task_type"], existing["id"],
            )
            return None
    except Exception:
        pass  # Non-critical guard — continue with extraction if check fails

    # ── Same-type novelty gate ───────────────────────────────────────────
    # Skip if a near-duplicate principle already exists for this task_type.
    # Fail-open when embeddings are unavailable (rate-limited below).
    embedder = embedding_provider if embedding_provider is not None else get_embedding_provider()
    is_novel, max_sim, principle_vec, fell_open = await _principle_is_novel(
        db,
        task_type=data["task_type"],
        new_principle=data["principle"],
        embedder=embedder,
        router=router,
        new_steps=data["steps"],
    )
    if not is_novel:
        logger.info(
            "Skipped extraction for %s: near-duplicate principle (cosine=%.3f >= %.2f)",
            data["task_type"], max_sim, NOVELTY_THRESHOLD,
        )
        return None

    # ── Fail-open rate limiter (FM5) ─────────────────────────────────────
    # When the embedder was unavailable, limit to one store per task_type
    # per cooldown window to prevent flooding during extended outages.
    if fell_open:
        now = time.monotonic()
        task_type_key = data["task_type"]
        if task_type_key in _fail_open_timestamps and now - _fail_open_timestamps[task_type_key] < FAIL_OPEN_COOLDOWN_SECS:
            logger.warning(
                "Fail-open rate limited for %s: cooldown active",
                task_type_key,
            )
            return None
        _fail_open_timestamps[task_type_key] = now

    # ── Cross-type contradiction check (FM2) ─────────────────────────────
    # After same-type novelty passes, check for trusted procedures with
    # overlapping context_tags across ALL task_types.
    # Jaccard threshold 0.5 (vs the CRUD default of 0.7) intentionally casts
    # a wider net — cross-type duplicates share domain but differ in name.
    try:
        from genesis.db.crud.procedural import find_by_context_overlap

        overlapping = await find_by_context_overlap(
            db, data["context_tags"], jaccard_threshold=0.5, limit=5,
        )
        for ov in overlapping:
            if (
                ov.get("confidence", 0) >= 0.5
                and ov.get("draft") == 0
                and principle_vec is not None
                and embedder is not None
            ):
                try:
                    ov_emb = await embedder.embed(ov["principle"])
                    sim = cosine_similarity(principle_vec, ov_emb)
                    if sim >= NOVELTY_THRESHOLD:
                        logger.info(
                            "Cross-type duplicate: '%s' vs existing '%s' "
                            "(cosine=%.3f)",
                            data["task_type"], ov["task_type"], sim,
                        )
                        return None
                    if sim < 0.3:
                        logger.warning(
                            "Potential cross-type contradiction: '%s' vs "
                            "'%s' (cosine=%.3f)",
                            data["task_type"], ov["task_type"], sim,
                        )
                except Exception:
                    pass  # Best-effort embedding comparison
    except Exception:
        pass  # Best-effort cross-check, never block extraction

    # ── Pack embedding & store ───────────────────────────────────────────
    principle_blob: bytes | None = None
    if principle_vec is not None:
        try:
            from genesis.learning.procedural.embedding import pack_embedding

            principle_blob = pack_embedding(principle_vec)
        except Exception:
            logger.warning(
                "Failed to pack principle embedding; storing without it",
                exc_info=True,
            )

    # ── Validation gate ───────────────────────────────────────────────────
    from genesis.learning.procedural.validation_gate import validate_extraction

    gate_result = await validate_extraction(
        db,
        task_type=data["task_type"],
        principle=data["principle"],
        steps=data["steps"],
        tools_used=data["tools_used"],
        outcome=outcome,
        summary_text=summary_text,
        session_tools_count=session_tools_count,
    )

    if not gate_result.allowed:
        logger.info(
            "Validation gate blocked extraction: task_type=%s flags=%s",
            data["task_type"], gate_result.flags,
        )
        # Emit J9 event for monitoring
        try:
            from genesis.eval.j9_hooks import emit_gate_decision

            await emit_gate_decision(
                db,
                task_type=data["task_type"],
                outcome=outcome,
                allowed=False,
                confidence=0.0,
                flags=gate_result.flags,
            )
        except Exception:
            pass  # Fire-and-forget
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
            activation_tier="LIBRARY",
            draft=1,
            confidence=gate_result.adjusted_confidence,
            source={"type": "auto_extracted", "triage_outcome": outcome},
            principle_embedding=principle_blob,
            extraction_context=gate_result.extraction_context,
            first_mover=1 if gate_result.first_mover else 0,
        )
        logger.info("Extracted procedure %s: %s (conf=%.2f)",
                    proc_id, data["task_type"], gate_result.adjusted_confidence)
        # Emit J9 event
        try:
            from genesis.eval.j9_hooks import emit_gate_decision

            await emit_gate_decision(
                db,
                task_type=data["task_type"],
                outcome=outcome,
                allowed=True,
                confidence=gate_result.adjusted_confidence,
                flags=gate_result.flags,
            )
        except Exception:
            pass  # Fire-and-forget
        return proc_id
    except Exception:
        logger.error("Procedure extraction: failed to store procedure", exc_info=True)
        return None
