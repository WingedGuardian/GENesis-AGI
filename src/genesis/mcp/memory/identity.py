"""Identity evolution tools."""

from __future__ import annotations

import logging

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod

logger = logging.getLogger(__name__)


@mcp.tool()
async def evolution_propose(
    proposal_type: str,
    current_content: str,
    proposed_change: str,
    rationale: str,
    source_reflection_id: str | None = None,
) -> str:
    """Write identity evolution proposal. Returns proposal ID (status: pending)."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    proposal_id = await memory_mod.evolution_proposals.create(
        memory_mod._db,
        proposal_type=proposal_type,
        current_content=current_content,
        proposed_change=proposed_change,
        rationale=rationale,
        source_reflection_id=source_reflection_id,
    )
    logger.info("Evolution proposal created: %s (type=%s)", proposal_id, proposal_type)
    return proposal_id
