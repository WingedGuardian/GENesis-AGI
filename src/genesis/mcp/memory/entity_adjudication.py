"""Entity-merge review surface: list proposals, approve/reject, apply.

The human-in-the-loop half of the entity-adjudication apply gate (PR-1 built the
CRUD + gated apply path; these tools wire them for a person). A ``proposed_merge``
is applied ONLY after ``entity_adjudication_approve`` sets ``approved_at`` and
``entity_adjudication_apply`` runs — nothing here auto-applies.
"""

from __future__ import annotations

import aiosqlite

from genesis.db.crud import entity_adjudications as adj_crud
from genesis.memory import entity_adjudication as adj

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod


# The HUMAN half of the entity-merge approval gate. approve/apply/reject each carry
# irreversible human-review authority (apply tombstones one entity into another; reject
# buries a proposal), so an autonomous/dispatched CC session must never call them — it
# would self-approve the gate. TWO layers, by design:
#   1. This wrapper pre-check returns a clean refusal dict on the normal MCP-tool path
#      (a friendly message to the calling LLM, no exception on the common path).
#   2. The REAL chokepoint is the guard_human_gate() call inside the CRUD/service
#      functions (adj_crud.approve/reject, apply_approved_merges) — it fires for ANY
#      caller, including a Bash-capable dispatched session that imports the function
#      directly (its subprocess inherits GENESIS_CC_SESSION=1). See
#      security/immunity_shadow.guard_human_gate.
# Mirrors the guard protecting the user-only ego tools (mcp/health/ego_tools.py:144).
# SCOPE (do not overstate): this closes the tool-call and naive-import self-approval
# paths. It is NOT a sandbox — a session that forges GENESIS_SESSION_SUPERVISED in a
# subprocess it spawns, or opens the SQLite file with raw SQL, bypasses any env-based
# gate. Containment of Bash-capable autonomous builders (Sentinel/Ego run unrestricted
# Bash with same-host DB access) is a separate architecture concern, tracked as a
# follow-up. The per-builder disallowed_tools denylists stay as CC-layer belt. Fail
# direction is safe: a foreground human terminal has no dispatch marker (or is
# supervised) → allowed; see is_dispatched_session_env.
def _refuse_if_dispatched(tool: str) -> dict | None:
    from genesis.security.immunity_shadow import is_dispatched_session_env

    if is_dispatched_session_env():
        return {
            "status": "refused",
            "reason": (
                f"{tool} is the human half of the entity-merge approval gate. An "
                "autonomous/dispatched session must not approve, apply, or reject a "
                "proposed entity merge (applying is an irreversible tombstone) — that "
                "is a human decision. Surface the proposal through your session's "
                "normal output instead; a person reviews it in a foreground session."
            ),
        }
    return None


async def _entity_brief(db: aiosqlite.Connection, entity_id: str) -> dict:
    """Lightweight display fields for one side of a proposed merge."""
    cur = await db.execute(
        "SELECT name, entity_type, summary, status FROM entities WHERE entity_id = ?",
        (entity_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return {"entity_id": entity_id, "missing": True}
    return {
        "entity_id": entity_id,
        "name": row[0],
        "type": row[1],
        "summary": row[2],
        "status": row[3],
    }


@mcp.tool()
async def entity_adjudication_list(status: str = "proposed", limit: int = 50) -> list[dict]:
    """List entity-merge proposals for human review.

    status: 'proposed' (unapproved, awaiting review), 'approved' (approved,
    awaiting apply), or 'all'. Each row is enriched with both entities' name/type/
    summary + the adjudicator's reasoning/provider so a human can judge the merge.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    db = memory_mod._db
    assert db is not None
    rows = await adj_crud.list_for_review(db, status=status, limit=limit)
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "pair_key": r["pair_key"],
                "norm_a": r["norm_a"],
                "norm_b": r["norm_b"],
                "reasoning": r["reasoning"],
                "provider": r["provider"],
                "created_at": r["created_at"],
                "approved_at": r["approved_at"],
                "loser_id": r["loser_id"],
                "survivor_id": r["survivor_id"],
                "entity_a": await _entity_brief(db, r["entity_a"]),
                "entity_b": await _entity_brief(db, r["entity_b"]),
            }
        )
    return out


@mcp.tool()
async def entity_adjudication_approve(pair_key: str, approved_by: str = "user") -> dict:
    """Approve a proposed entity merge for application. Does NOT apply it — run
    ``entity_adjudication_apply`` afterwards. Idempotent."""
    if (refused := _refuse_if_dispatched("entity_adjudication_approve")) is not None:
        return refused
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    moved = await adj_crud.approve(memory_mod._db, pair_key=pair_key, approved_by=approved_by)
    return {"pair_key": pair_key, "approved": moved}


@mcp.tool()
async def entity_adjudication_reject(pair_key: str, reason: str) -> dict:
    """Reject a proposed entity merge: records it as 'distinct' so it is never
    applied and never re-nominated by the sweep."""
    if (refused := _refuse_if_dispatched("entity_adjudication_reject")) is not None:
        return refused
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    moved = await adj_crud.reject(memory_mod._db, pair_key=pair_key, reason=reason)
    return {"pair_key": pair_key, "rejected": moved}


@mcp.tool()
async def entity_adjudication_apply(budget: int = 50) -> dict:
    """Apply all human-APPROVED entity merges (up to ``budget``), mode-independent.

    Only rows with ``approved_at`` set are applied; the staleness guard still runs.
    Returns a counts summary: {'merged': N, 'stale': M}.
    """
    if (refused := _refuse_if_dispatched("entity_adjudication_apply")) is not None:
        return refused
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    return await adj.apply_approved_merges(memory_mod._db, budget=budget)
