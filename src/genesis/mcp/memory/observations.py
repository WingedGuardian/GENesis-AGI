"""Observation tools: write, query, resolve."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from genesis.db.crud import observations
from genesis.memory.provenance import (
    ORIGIN_FIRST_PARTY,
    ORIGIN_OWNER,
    namespace_untrusted_observation,
    session_origin_from_env,
)

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
    """Write processed reflection/observation. Returns observation_id.

    From a session running over untrusted content, the row is stored with
    ``untrusted:`` in front of its type, source and category, and ``critical``
    priority becomes ``high`` (``provenance.namespace_untrusted_observation``),
    so it can never pass for one of Genesis's own pipeline rows.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    origin = session_origin_from_env() or ORIGIN_FIRST_PARTY
    if origin not in (ORIGIN_OWNER, ORIGIN_FIRST_PARTY):
        source, type, category, priority = namespace_untrusted_observation(
            source=source, type_=type, category=category, priority=priority,
        )
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
        origin_class=origin,
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
    """Query observations by type/priority/source."""
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
    """Mark observation resolved with notes."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    return await observations.resolve(
        memory_mod._db,
        observation_id,
        resolved_at=datetime.now(UTC).isoformat(),
        resolution_notes=resolution_notes,
    )
