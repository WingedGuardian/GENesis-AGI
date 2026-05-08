"""Procedural memory tools: store, recall."""

from __future__ import annotations

from dataclasses import asdict as _asdict

from genesis.learning.procedural.matcher import find_best_match, find_relevant

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod


@mcp.tool()
async def procedure_store(
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    tool_trigger: list[str] | None = None,
) -> str:
    """Store a learned procedure. Returns the procedure ID.

    An MCP `procedure_store` call represents an *explicit teach* — the caller
    is asserting the procedure works. We seed it as already-confirmed
    (speculative=0) with one Laplace-equivalent success (success_count=1,
    confidence=2/3), and place it at L3 so it is immediately recallable AND
    eligible for SessionStart injection. Subsequent organic
    successes/failures via `record_success`/`record_failure` continue to
    update the row via Laplace smoothing.

    The auto-extraction path (`learning.procedural.extractor`) keeps its
    speculative=1 / success_count=0 / confidence=0.0 / L4 defaults — those
    procedures are LLM-hypothesized and must earn trust.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    from genesis.learning.procedural.operations import store_procedure_checked

    result = await store_procedure_checked(
        memory_mod._db,
        task_type=task_type,
        principle=principle,
        steps=steps,
        tools_used=tools_used,
        context_tags=context_tags,
        tool_trigger=tool_trigger,
        activation_tier="L3",
        speculative=0,
        success_count=1,
        confidence=2 / 3,
        source={"type": "explicit_teach"},
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

    results = []

    if context_tags:
        match = await find_best_match(memory_mod._db, task_description, context_tags)
        if match:
            results.append(_asdict(match))

    tags = context_tags or task_description.lower().replace("-", " ").split()
    relevant = await find_relevant(memory_mod._db, tags, limit=3)
    for m in relevant:
        if not any(r.get("procedure_id") == m.procedure_id for r in results):
            results.append(_asdict(m))
        if len(results) >= 3:
            break

    # J-9 eval: log procedure invocations for learning effectiveness tracking
    if results:
        from genesis.eval.j9_hooks import emit_procedure_invoked
        for r in results:
            await emit_procedure_invoked(
                memory_mod._db,
                procedure_id=r.get("procedure_id", ""),
                confidence=r.get("confidence", 0.0),
                matched_tags=tags[:10] if tags else [],
            )

    return results
