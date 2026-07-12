"""Procedural memory tools: store, recall."""

from __future__ import annotations

import logging
from dataclasses import asdict as _asdict

from genesis.learning.procedural.matcher import find_best_match, find_relevant

from ..memory import mcp

logger = logging.getLogger(__name__)


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod


def _rank_by_effective_confidence(results: list[dict]) -> list[dict]:
    """Re-rank recall results so deliberately-read procedures surface first.

    Reads count as fractional successes via ``effective_confidence``; a stable
    sort preserves ``find_relevant``'s relevance order on ties. Isolated to the
    recall path — ``find_relevant`` itself is unchanged (6 callers, incl.
    autonomy outcome-attribution).
    """
    from genesis.learning.procedural.operations import effective_confidence

    return sorted(
        results,
        key=lambda r: effective_confidence(
            r.get("success_count", 0) or 0,
            r.get("failure_count", 0) or 0,
            r.get("invocation_count", 0) or 0,
        ),
        reverse=True,
    )


async def _embed_principle_for_hook(principle: str) -> bytes | None:
    """Best-effort: compute principle embedding for the proactive procedure
    hook. Returns None on any failure — the hook simply skips rows without
    an embedding rather than failing the whole store.
    """
    try:
        from genesis.learning.procedural.embedding import pack_embedding
        from genesis.memory.embeddings import EmbeddingProvider

        embedder = EmbeddingProvider()
        vec = await embedder.embed(principle)
        return pack_embedding(vec)
    except Exception:
        logger.warning(
            "procedure_store: principle embedding failed, storing without it",
            exc_info=True,
        )
        return None


@mcp.tool()
async def procedure_store(
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    scenario: str | None = None,
    tool_trigger: list[str] | None = None,
) -> str:
    """Store a learned procedure. Returns the procedure ID.

    An MCP `procedure_store` call represents an *explicit teach* — the caller
    is asserting the procedure works. We seed it as already-confirmed
    (draft=0) with one Laplace-equivalent success (success_count=1,
    confidence=2/3), and place it at LIBRARY so it is immediately
    recallable and eligible for proactive-hook surfacing. (Blind SessionStart
    injection is CORE-only as of Surfacing v2, so an explicit teach reaches
    a session via the proactive hook on the first prompt, not at session start.)
    Subsequent organic successes/failures via `record_success`/`record_failure`
    continue to update the row via Laplace smoothing.

    Computes a principle embedding for the proactive procedure hook. If the
    embedding stack is unavailable, the procedure stores without it and the
    hook skips that row.

    The auto-extraction path (`learning.procedural.extractor`) keeps its
    draft=1 / success_count=0 / confidence=0.0 / DORMANT defaults — those
    procedures are LLM-hypothesized and must earn trust.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    from genesis.learning.procedural.operations import store_procedure_checked

    principle_blob = await _embed_principle_for_hook(principle)

    result = await store_procedure_checked(
        memory_mod._db,
        task_type=task_type,
        principle=principle,
        scenario=scenario,
        steps=steps,
        tools_used=tools_used,
        context_tags=context_tags,
        tool_trigger=tool_trigger,
        activation_tier="LIBRARY",
        draft=0,
        success_count=1,
        confidence=2 / 3,
        source={"type": "explicit_teach"},
        principle_embedding=principle_blob,
    )

    # WS-3 B1 gate-1 (procedure): shadow-record the teach, classified by the
    # CALLER session's origin (GENESIS_SESSION_ORIGIN env — this MCP tool is
    # exposed in research/campaign profiles alongside web tools, so an
    # external-influenced background session can teach a procedure; the taught
    # tools_used is REPLAY tools, not caller provenance). MUST coalesce the env
    # read: the gate normalizes None ADVERSARIALLY (is_blockable(None) → True),
    # so a raw None would record a false would-block row for EVERY internal
    # teach — unset env = not a dispatched session = first_party (never owner:
    # reflection/ego/sentinel profiles also reach this tool with the env unset).
    # Skipped duplicate teaches promote nothing → no emit.
    if result.action != "skipped":
        from genesis.memory.provenance import (
            ORIGIN_FIRST_PARTY,
            session_origin_from_env,
        )
        from genesis.security import immunity_shadow

        await immunity_shadow.record_would_block(
            gate="procedure",
            source_kind="procedure_teach",
            source_ref="mcp/memory/procedural.py::procedure_store",
            process="server",
            blockable_count=1,
            origin_class=session_origin_from_env() or ORIGIN_FIRST_PARTY,
            db=memory_mod._db,
            detail={"procedure_id": result.procedure_id, "action": result.action},
        )

    response = result.procedure_id
    if result.action == "updated":
        response = f"Updated existing procedure {result.procedure_id} (version bumped)"
    elif result.action == "skipped":
        response = f"Skipped: procedure {result.conflicting_ids[0]} already covers this task_type"
    if result.warnings:
        response += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in result.warnings)
    return response


@mcp.tool()
async def procedure_recall(
    task_description: str,
    context_tags: list[str] | None = None,
) -> list[dict]:
    """Find learned procedures matching a task description."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None

    results: list[dict] = []
    seen: set[str] = set()

    if context_tags:
        match = await find_best_match(memory_mod._db, task_description, context_tags)
        if match:
            results.append(_asdict(match))
            seen.add(match.procedure_id)

    tags = context_tags or task_description.lower().replace("-", " ").split()
    # Widen the candidate pool, then re-rank by reads (effective confidence) and
    # cap — so a proven-useful procedure can surface, not just get reordered
    # within an already-relevance-capped top 3.
    relevant = await find_relevant(memory_mod._db, tags, limit=10)
    for m in relevant:
        if m.procedure_id not in seen:
            results.append(_asdict(m))
            seen.add(m.procedure_id)

    results = _rank_by_effective_confidence(results)[:3]

    # Count the read (usage signal) + log the J-9 invocation event. Returning a
    # procedure means the model recalled it and it is surfaced into context.
    if results:
        from genesis.db.crud import procedural
        from genesis.eval.j9_hooks import emit_procedure_invoked

        for r in results:
            pid = r.get("procedure_id", "")
            if not pid:
                continue
            # Keep the read counter and the J-9 event in lockstep — both gated
            # on a real procedure_id so neither records an orphan.
            await procedural.record_invocation(memory_mod._db, pid)
            await emit_procedure_invoked(
                memory_mod._db,
                procedure_id=pid,
                confidence=r.get("confidence", 0.0),
                matched_tags=tags[:10] if tags else [],
            )

    return results
