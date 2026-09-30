"""Observation tools: write, query, resolve."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from genesis.db.crud import observations
from genesis.memory.provenance import ORIGIN_FIRST_PARTY, session_origin_from_env

from ..memory import mcp

query = observations.query
create = observations.create
resolve = observations.resolve


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod


@mcp.tool()
async def observation_write(
    content: str,
    source: str,
    type: str,
    priority: str = "medium",
    category: str | None = None,
    speculative: bool = False,
) -> str:
    """Record an observation: a typed, prioritized note for Genesis's cognitive loops.

    Use for signals other subsystems should act on or surface later (e.g. a
    ``task_detected`` from conversation, a ``user_signal`` from an inbox
    evaluation). Durable facts and decisions belong in ``memory_store`` instead.

    ``source`` names the writer (e.g. ``conversation_intent``,
    ``inbox_evaluation``); ``type`` is a free-form kind that also sets its
    lifetime: most types expire and are auto-resolved after a type-specific
    TTL (unlisted types after 14 days). The few types with no TTL still get
    auto-resolved after 60 days at low/medium priority; only high/critical
    stay until resolved by hand.
    ``priority`` must be one of low / medium / high / critical;
    any other value fails the write. ``speculative`` marks an unverified
    inference. The writer's session origin is stamped automatically.

    Returns the new observation_id, or ``"duplicate_skipped"`` when an unresolved
    observation with the same source, identical content and the same writer
    origin already exists.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    result = await observations.create(
        memory_mod._db,
        id=str(uuid.uuid4()),
        source=source,
        type=type,
        content=content,
        priority=priority,
        created_at=datetime.now(UTC).isoformat(),
        category=category,
        speculative=int(speculative),
        # WS-3: stamp the dispatching session's origin (mirrors memory_store /
        # procedure_store / knowledge writers), so an external-origin session
        # (e.g. the inbox judge over untrusted content) can no longer forge a
        # privileged-looking observation — a NULL origin used to read as
        # first-party "by omission" and slip past the user-model consumer gate.
        # Coalesce None → first_party (server/foreground writers); the gate
        # normalizes adversarially, so a raw None must never be forwarded.
        origin_class=session_origin_from_env() or ORIGIN_FIRST_PARTY,
        skip_if_duplicate=True,
    )
    return result or "duplicate_skipped"


@mcp.tool()
async def observation_query(
    type: str | None = None,
    priority: str | None = None,
    source: str | None = None,
    resolved: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    """List observations, newest first, filtered by type, priority, source, or resolved state.

    Returns at most ``limit`` rows (default 50; must be 1 or more) and no total
    count, so a result of exactly ``limit`` rows may be truncated: raise
    ``limit`` or narrow the filters before concluding something is absent. Each
    row includes its id, for ``observation_resolve``.
    """
    # SQLite reads a negative LIMIT as "no limit", which would turn this bounded
    # page into the whole table.
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return [{"error": "limit must be an integer of 1 or more"}]
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    return await observations.query(
        memory_mod._db,
        type=type,
        priority=priority,
        source=source,
        resolved=resolved,
        limit=limit,
    )


@mcp.tool()
async def observation_resolve(
    observation_id: str,
    resolution_notes: str,
) -> bool:
    """Mark one observation resolved, recording why in ``resolution_notes``.

    Resolved observations stop surfacing and no longer block a duplicate write
    of the same content. Returns True if the observation was found and updated.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    return await observations.resolve(
        memory_mod._db,
        observation_id,
        resolved_at=datetime.now(UTC).isoformat(),
        resolution_notes=resolution_notes,
    )
