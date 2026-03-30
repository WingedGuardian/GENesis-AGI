"""MCP tools for skill proposal management."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from genesis.db.crud import observations
from genesis.mcp.memory import mcp


def _memory_mod():  # type: ignore[no-untyped-def]
    import genesis.mcp.memory as memory_mod

    return memory_mod


@mcp.tool()
async def skill_proposal_list(
    resolved: bool | None = False,
    limit: int = 20,
) -> list[dict]:
    """List pending skill improvement proposals.

    Returns observations of type 'skill_proposal', newest first.
    Set resolved=True to see past proposals, or resolved=None for all.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    rows = await observations.query(
        memory_mod._db,
        type="skill_proposal",
        source="skill_evolution",
        resolved=resolved,
        limit=limit,
    )
    # Parse content JSON for readability
    for row in rows:
        try:
            row["parsed"] = json.loads(row.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["parsed"] = {}
    return rows


@mcp.tool()
async def skill_proposal_accept(
    observation_id: str,
) -> str:
    """Accept a skill proposal — re-runs the refiner and applies changes.

    The proposal must be an unresolved observation of type 'skill_proposal'.
    Triggers the skill evolution pipeline to regenerate and apply the proposal.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None

    # Fetch the proposal
    rows = await observations.query(
        memory_mod._db, type="skill_proposal", resolved=False, limit=100,
    )
    proposal_row = next((r for r in rows if r["id"] == observation_id), None)
    if proposal_row is None:
        return f"Proposal {observation_id} not found or already resolved."

    try:
        data = json.loads(proposal_row["content"])
    except (json.JSONDecodeError, TypeError):
        return "Failed to parse proposal content."

    skill_name = data.get("skill_name", "")
    if not skill_name:
        return "Proposal missing skill_name."

    # Re-run the pipeline for this specific skill to generate and apply
    applied = False
    try:
        from genesis.learning.skills.pipeline import SkillEvolutionPipeline
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        pipeline = SkillEvolutionPipeline(db=memory_mod._db, router=rt._router)
        result = await pipeline.propose_for_skill(skill_name)
        applied = result is not None and result.get("action") == "applied"
    except Exception:
        pass  # Pipeline unavailable — resolve observation anyway

    # Resolve the observation
    action = "applied" if applied else "accepted_pending_apply"
    await observations.resolve(
        memory_mod._db,
        observation_id,
        resolved_at=datetime.now(UTC).isoformat(),
        resolution_notes=f"{action} via MCP tool. Skill: {skill_name}",
    )

    # Log acceptance
    await observations.create(
        memory_mod._db,
        id=str(uuid.uuid4()),
        source="skill_evolution",
        type="skill_evolution",
        content=json.dumps({
            "skill_name": skill_name,
            "action": action,
            "rationale": data.get("rationale", ""),
        }),
        priority="medium",
        created_at=datetime.now(UTC).isoformat(),
    )

    if applied:
        return f"Proposal for '{skill_name}' accepted and applied to SKILL.md."
    return (
        f"Proposal for '{skill_name}' accepted. Pipeline could not auto-apply "
        f"(router unavailable or refiner returned no changes). "
        f"Manual SKILL.md edit may be needed."
    )


@mcp.tool()
async def skill_proposal_reject(
    observation_id: str,
    reason: str = "Rejected by user",
) -> str:
    """Reject a skill proposal — resolves the observation without applying changes."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None

    success = await observations.resolve(
        memory_mod._db,
        observation_id,
        resolved_at=datetime.now(UTC).isoformat(),
        resolution_notes=f"Rejected: {reason}",
    )
    if success:
        return f"Proposal {observation_id} rejected: {reason}"
    return f"Proposal {observation_id} not found."
