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
    """Write processed reflection/observation. Returns observation_id."""
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
