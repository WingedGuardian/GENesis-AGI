"""Loop-closure funnel aggregations — READ-ONLY.

Per-artifact "is the self-learning loop closed?" funnel —
captured → surfaced/invoked → actuated → measured → leak — computed entirely
from existing tables. No writes, no schema changes. Powers the
``loop_closure_status`` MCP tool (the self-learning health surface, which
subsumes ``self_improvement_status``).

The point is honest accounting: a thing that is *captured* but never *acted on*
or *measured* is a leak — work/learning that fell through the cracks. These
queries count exactly that, per artifact, so the operator (and Genesis) can see
where the loop is open.

"Stale" thresholds are passed in by the caller (ISO cutoff) so these functions
stay deterministic and unit-testable — no wall-clock inside the query layer.
All ``created_at`` writers use ``datetime.now(UTC).isoformat()`` (fixed-width,
same zone), so the lexicographic ``created_at < ?`` compare == chronological.
"""

from __future__ import annotations

import aiosqlite


async def _scalar(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> int:
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def _group_counts(db: aiosqlite.Connection, sql: str) -> dict[str, int]:
    cur = await db.execute(sql)
    return {(r[0] or "∅"): r[1] for r in await cur.fetchall()}


def _loop_label(total: int, *, flowing: int, leaked: int) -> str:
    """Data-derived loop status (never hardcoded):

    EMPTY   — nothing captured yet
    OPEN    — captured but nothing is flowing through (acted on / measured)
    PARTIAL — some flows, some leaks
    CLOSED  — everything captured has flowed through, no leak
    """
    if total == 0:
        return "EMPTY"
    if flowing == 0:
        return "OPEN"
    return "PARTIAL" if leaked > 0 else "CLOSED"


async def procedure_funnel(db: aiosqlite.Connection) -> dict:
    """Procedures are outcome-graded, but ``procedural_memory`` has NO 'surfaced'
    counter — ``invocation_count`` is the actuation signal (the procedure
    actually fired). So we report ``invoked`` (not 'surfaced'); the leak is
    captured-but-never-invoked (the golden-dormant that never gets to mature)."""
    total = await _scalar(db, "SELECT COUNT(*) FROM procedural_memory")
    invoked = await _scalar(
        db, "SELECT COUNT(*) FROM procedural_memory WHERE invocation_count > 0"
    )
    measured = await _scalar(
        db,
        "SELECT COUNT(*) FROM procedural_memory WHERE success_count + failure_count > 0",
    )
    deprecated = await _scalar(
        db, "SELECT COUNT(*) FROM procedural_memory WHERE deprecated = 1"
    )
    by_tier = await _group_counts(
        db, "SELECT activation_tier, COUNT(*) FROM procedural_memory GROUP BY activation_tier"
    )
    leak_uninvoked = total - invoked
    return {
        "artifact": "procedure",
        "captured": total,
        "invoked": invoked,
        "measured": measured,
        "by_tier": by_tier,
        "deprecated": deprecated,
        "leak_uninvoked": leak_uninvoked,
        "loop": _loop_label(total, flowing=invoked, leaked=leak_uninvoked),
    }


async def observation_funnel(db: aiosqlite.Connection, *, stale_before: str) -> dict:
    """Observations: actuation signal = ``influenced_action``. Leak = unresolved,
    un-actuated, and aged out."""
    total = await _scalar(db, "SELECT COUNT(*) FROM observations")
    surfaced = await _scalar(
        db, "SELECT COUNT(*) FROM observations WHERE surfaced_count > 0"
    )
    actuated = await _scalar(
        db, "SELECT COUNT(*) FROM observations WHERE influenced_action = 1"
    )
    resolved = await _scalar(db, "SELECT COUNT(*) FROM observations WHERE resolved = 1")
    leak_stale = await _scalar(
        db,
        "SELECT COUNT(*) FROM observations "
        "WHERE resolved = 0 AND influenced_action = 0 AND created_at < ?",
        (stale_before,),
    )
    return {
        "artifact": "observation",
        "captured": total,
        "surfaced": surfaced,
        "actuated": actuated,
        "resolved": resolved,
        "leak_stale_unactuated": leak_stale,
        "loop": _loop_label(total, flowing=actuated, leaked=leak_stale),
    }


async def reflection_funnel(db: aiosqlite.Connection) -> dict:
    """Reflections: actuation signal = ``used_in_optimization``. Leak = generated
    but never used to change anything."""
    total = await _scalar(db, "SELECT COUNT(*) FROM reflection_corpus")
    graded = await _scalar(
        db, "SELECT COUNT(*) FROM reflection_corpus WHERE quality_label IS NOT NULL"
    )
    used = await _scalar(
        db, "SELECT COUNT(*) FROM reflection_corpus WHERE used_in_optimization = 1"
    )
    leak_unused = total - used
    return {
        "artifact": "reflection",
        "captured": total,
        "graded": graded,
        "actuated": used,
        "leak_unused": leak_unused,
        "loop": _loop_label(total, flowing=used, leaked=leak_unused),
    }


async def followup_funnel(db: aiosqlite.Connection, *, stale_before: str) -> dict:
    """Follow-ups: actuated = past the queue (``scheduled``/``in_progress``/
    ``completed``). Leak = ``pending`` past the stale cutoff (the graveyard)."""
    by_status = await _group_counts(
        db, "SELECT status, COUNT(*) FROM follow_ups GROUP BY status"
    )
    total = sum(by_status.values())
    actuated = (
        by_status.get("scheduled", 0)
        + by_status.get("in_progress", 0)
        + by_status.get("completed", 0)
    )
    pending_stale = await _scalar(
        db,
        "SELECT COUNT(*) FROM follow_ups WHERE status = 'pending' AND created_at < ?",
        (stale_before,),
    )
    return {
        "artifact": "follow_up",
        "captured": total,
        "by_status": by_status,
        "actuated": actuated,
        "leak_pending_stale": pending_stale,
        "loop": _loop_label(total, flowing=actuated, leaked=pending_stale),
    }


async def proposal_funnel(db: aiosqlite.Connection, *, stale_before: str) -> dict:
    """Ego proposals: actuated = sanctioned for action (``approved``/``executed``).
    Leak = ``pending`` past the stale cutoff (approval never came)."""
    by_status = await _group_counts(
        db, "SELECT status, COUNT(*) FROM ego_proposals GROUP BY status"
    )
    total = sum(by_status.values())
    actuated = by_status.get("approved", 0) + by_status.get("executed", 0)
    pending_stale = await _scalar(
        db,
        "SELECT COUNT(*) FROM ego_proposals WHERE status = 'pending' AND created_at < ?",
        (stale_before,),
    )
    return {
        "artifact": "ego_proposal",
        "captured": total,
        "by_status": by_status,
        "actuated": actuated,
        "leak_pending_stale": pending_stale,
        "loop": _loop_label(total, flowing=actuated, leaked=pending_stale),
    }
