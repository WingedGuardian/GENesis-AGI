"""Cognitive self-modification ledger MCP tools — operator surface.

Read + rollback over the ``cognitive_file_modifications`` ledger (skill / triage
calibration / user-knowledge self-edits). ``cognitive_modification_status`` is
read-only; ``cognitive_modification_rollback`` reverts one recorded modification to
its pre-image (drift-guarded — refuses if the file changed since, unless ``force``).

The status tool truncates file contents to character counts (the full pre/post images
live in the DB) so the response stays small.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _service_db():
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return None
    return _service._db


async def _impl_cognitive_modification_status(
    limit: int = 20, actor: str | None = None,
) -> dict:
    db = _service_db()
    if db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    from genesis.db.crud import cognitive_file_modifications as cfm

    by_target = await cfm.counts_by_target(db)
    rows = await cfm.recent(db, limit=limit, actor=actor)
    recent_view = [
        {
            "mod_id": r["id"],
            "actor": r["actor"],
            "target_path": r["target_path"],
            "status": r["status"],
            "created_at": r["created_at"],
            "rolled_back_at": r.get("rolled_back_at"),
            "summary": r.get("change_summary"),
            "prior_chars": len(r["prior_content"]) if r.get("prior_content") else 0,
            "applied_chars": len(r["applied_content"]) if r.get("applied_content") else 0,
        }
        for r in rows
    ]
    return {
        "status": "ok",
        "total_targets": len(by_target),
        "by_target": by_target,
        "recent": recent_view,
        "note": (
            "Operator surface for autonomous cognitive self-modifications. To revert "
            "one, call cognitive_modification_rollback(mod_id). Reading this changes "
            "nothing. Each target keeps its last 30 modifications."
        ),
    }


async def _impl_cognitive_modification_rollback(
    mod_id: str, force: bool = False,
) -> dict:
    db = _service_db()
    if db is None:
        return {"ok": False, "status": "unavailable", "message": "DB not initialized"}

    from genesis.learning.cognitive_ledger import rollback

    return await rollback(db, mod_id, force=force)


@mcp.tool()
async def cognitive_modification_status(
    limit: int = 20, actor: str | None = None,
) -> dict:
    """What cognitive config files has Genesis autonomously overwritten, and can they
    be rolled back?

    Read-only view of the cognitive self-modification ledger: per-file modification
    counts and the most-recent edits (skill SKILL.md refinement, daily
    TRIAGE_CALIBRATION.md / USER_KNOWLEDGE.md regen) with their ``mod_id`` for
    rollback. Filter by ``actor`` (skill_evolution / triage_calibration_daily /
    user_model_evolution). Reading this changes nothing.
    """
    return await _impl_cognitive_modification_status(limit=limit, actor=actor)


@mcp.tool()
async def cognitive_modification_rollback(mod_id: str, force: bool = False) -> dict:
    """Revert one autonomous cognitive file modification to its prior contents.

    Restores the pre-image captured before the edit identified by ``mod_id`` (from
    cognitive_modification_status). Drift-guarded: if the file changed since (a later
    regen overwrote it), the rollback is REFUSED — roll back the newer entry, or pass
    ``force=True`` to override. Emits an observation on every outcome.
    """
    return await _impl_cognitive_modification_rollback(mod_id, force=force)
