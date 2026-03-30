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
    """Store a learned procedure. Returns the procedure ID."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    from genesis.learning.procedural.operations import store_procedure

    return await store_procedure(
        memory_mod._db,
        task_type=task_type,
        principle=principle,
        steps=steps,
        tools_used=tools_used,
        context_tags=context_tags,
        tool_trigger=tool_trigger,
        activation_tier="L4",
        speculative=1,
    )


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

    return results
