"""CRUD operations for ego_cycles, ego_proposals, and ego_state tables."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ego_cycles
# ---------------------------------------------------------------------------


async def create_cycle(
    db: aiosqlite.Connection,
    *,
    id: str,
    output_text: str,
    proposals_json: str = "[]",
    focus_summary: str = "",
    model_used: str = "",
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
    created_at: str | None = None,
) -> str:
    """Insert a new ego cycle record. Returns the id."""
    if created_at is None:
        created_at = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO ego_cycles
           (id, output_text, proposals_json, focus_summary,
            model_used, cost_usd, input_tokens, output_tokens,
            duration_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            id,
            output_text,
            proposals_json,
            focus_summary,
            model_used,
            cost_usd,
            input_tokens,
            output_tokens,
            duration_ms,
            created_at,
        ),
    )
    await db.commit()
    return id


async def get_cycle(db: aiosqlite.Connection, id: str) -> dict | None:
    """Fetch a single cycle by id. Returns None if not found."""
    cursor = await db.execute(
        "SELECT * FROM ego_cycles WHERE id = ?",
        (id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def list_recent_cycles(
    db: aiosqlite.Connection,
    *,
    limit: int = 10,
) -> list[dict]:
    """Most recent cycles, newest first."""
    cursor = await db.execute(
        "SELECT * FROM ego_cycles ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def list_uncompacted_beyond_window(
    db: aiosqlite.Connection,
    *,
    window_size: int = 10,
) -> list[dict]:
    """Uncompacted cycles outside the recent window, oldest first.

    Returns cycles where ``compacted_into IS NULL`` excluding the most
    recent *window_size* uncompacted cycles.  These are the candidates
    for compaction (oldest first so callers compact incrementally).

    Secondary sort on ``id`` breaks ties when ``created_at`` values match.
    """
    cursor = await db.execute(
        """SELECT * FROM ego_cycles
           WHERE compacted_into IS NULL
             AND id NOT IN (
                 SELECT id FROM ego_cycles
                 WHERE compacted_into IS NULL
                 ORDER BY created_at DESC, id DESC
                 LIMIT ?
             )
           ORDER BY created_at ASC, id ASC""",
        (window_size,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def mark_compacted(
    db: aiosqlite.Connection,
    *,
    cycle_id: str,
    compacted_into: str,
) -> bool:
    """Set ``compacted_into`` on a cycle. Returns True if a row was updated."""
    cursor = await db.execute(
        "UPDATE ego_cycles SET compacted_into = ? WHERE id = ?",
        (compacted_into, cycle_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def count_uncompacted(db: aiosqlite.Connection) -> int:
    """Count cycles where ``compacted_into IS NULL``."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM ego_cycles WHERE compacted_into IS NULL",
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# ego_state  (key-value store)
# ---------------------------------------------------------------------------


async def get_state(db: aiosqlite.Connection, key: str) -> str | None:
    """Get a value from ego_state. Returns None if key not found."""
    cursor = await db.execute(
        "SELECT value FROM ego_state WHERE key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_state(
    db: aiosqlite.Connection,
    *,
    key: str,
    value: str,
) -> None:
    """Upsert a key-value pair in ego_state.

    Uses ON CONFLICT to guarantee ``updated_at`` is refreshed on every
    call.  ``INSERT OR REPLACE`` does NOT re-fire DEFAULT expressions —
    verified against live SQLite.
    """
    await db.execute(
        """INSERT INTO ego_state (key, value, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET
             value = excluded.value,
             updated_at = datetime('now')""",
        (key, value),
    )
    await db.commit()


_VALID_MODES = frozenset({"active", "low_activity", "urgent"})


async def get_mode(db: aiosqlite.Connection, ego_key: str = "ego_mode") -> str:
    """Get the ego operating mode. Defaults to 'active'."""
    mode = await get_state(db, ego_key)
    if not mode:
        return "active"
    base = mode.split(":")[0]
    if base == "focused" and ":" not in mode:
        return "active"
    return mode if base in (_VALID_MODES | {"focused"}) else "active"


async def set_mode(db: aiosqlite.Connection, mode: str, ego_key: str = "ego_mode") -> None:
    """Set the ego operating mode. Validates mode value.

    Valid modes: active, focused:<topic>, low_activity, urgent.
    """
    base = mode.split(":")[0]
    if base not in (_VALID_MODES | {"focused"}):
        raise ValueError(f"Invalid ego mode: {mode!r}")
    if base == "focused" and ":" not in mode:
        raise ValueError("focused mode requires a topic: 'focused:<topic>'")
    await set_state(db, key=ego_key, value=mode)


# ---------------------------------------------------------------------------
# ego_proposals
# ---------------------------------------------------------------------------


async def create_proposal(
    db: aiosqlite.Connection,
    *,
    id: str,
    action_type: str,
    action_category: str = "",
    content: str,
    rationale: str = "",
    confidence: float = 0.0,
    urgency: str = "normal",
    alternatives: str = "",
    status: str = "pending",
    cycle_id: str | None = None,
    batch_id: str | None = None,
    created_at: str | None = None,
    expires_at: str | None = None,
    rank: int | None = None,
    execution_plan: str | None = None,
    recurring: bool = False,
    memory_basis: str = "",
    realist_verdict: str | None = None,
    realist_reasoning: str | None = None,
    ego_source: str | None = None,
) -> str:
    """Insert a new ego proposal. Returns the id."""
    if created_at is None:
        created_at = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO ego_proposals
           (id, action_type, action_category, content, rationale,
            confidence, urgency, alternatives, status, cycle_id,
            batch_id, created_at, expires_at, rank, execution_plan,
            recurring, memory_basis, realist_verdict, realist_reasoning,
            ego_source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            id,
            action_type,
            action_category,
            content,
            rationale,
            confidence,
            urgency,
            alternatives,
            status,
            cycle_id,
            batch_id,
            created_at,
            expires_at,
            rank,
            execution_plan,
            1 if recurring else 0,
            memory_basis,
            realist_verdict,
            realist_reasoning,
            ego_source,
        ),
    )
    await db.commit()
    return id


async def get_proposal(db: aiosqlite.Connection, id: str) -> dict | None:
    """Fetch a single proposal by id."""
    cursor = await db.execute(
        "SELECT * FROM ego_proposals WHERE id = ?",
        (id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_proposals_by_batch(
    db: aiosqlite.Connection,
    batch_id: str,
) -> list[dict]:
    """All proposals in a batch, ordered by rowid (insertion order)."""
    cursor = await db.execute(
        "SELECT * FROM ego_proposals WHERE batch_id = ? ORDER BY rowid",
        (batch_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_pending_proposals(
    db: aiosqlite.Connection,
    *,
    ego_source: str | None = None,
) -> list[dict]:
    """All pending proposals, oldest first.

    If ``ego_source`` is provided, only returns proposals from that ego.
    NULL ego_source proposals (pre-migration) match any filter.
    """
    if ego_source:
        cursor = await db.execute(
            "SELECT * FROM ego_proposals "
            "WHERE status = 'pending' AND (ego_source = ? OR ego_source IS NULL) "
            "ORDER BY created_at ASC",
            (ego_source,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM ego_proposals WHERE status = 'pending' ORDER BY created_at ASC",
        )
    return [dict(r) for r in await cursor.fetchall()]


async def list_proposals(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """All proposals, optionally filtered by status, newest first."""
    if status:
        cursor = await db.execute(
            "SELECT * FROM ego_proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM ego_proposals ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def resolve_proposal(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
    user_response: str | None = None,
    resolved_at: str | None = None,
) -> bool:
    """Update a proposal's status. Returns True if a row was updated."""
    if resolved_at is None:
        resolved_at = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE ego_proposals SET status = ?, user_response = ?, resolved_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (status, user_response, resolved_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def execute_proposal(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
    user_response: str | None = None,
) -> bool:
    """Transition an approved proposal to executed or failed.

    Only operates on proposals with status='approved'. Companion to
    resolve_proposal (which operates on 'pending').
    """
    if status not in ("executed", "failed"):
        raise ValueError(f"execute_proposal status must be 'executed' or 'failed', got {status!r}")
    cursor = await db.execute(
        "UPDATE ego_proposals SET status = ?, user_response = ?, resolved_at = ? "
        "WHERE id = ? AND status = 'approved'",
        (status, user_response, datetime.now(UTC).isoformat(), id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_proposal_outcome(
    db: aiosqlite.Connection,
    proposal_id: str,
    *,
    success: bool,
    summary: str,
) -> bool:
    """Append session outcome to an executed proposal's user_response.

    Called after the dispatched session completes, so the ego knows whether
    the action actually succeeded or failed.
    """
    suffix = f"|{'completed' if success else 'failed'}:{summary[:200]}"
    cursor = await db.execute(
        "UPDATE ego_proposals SET user_response = user_response || ? "
        "WHERE id = ? AND status = 'executed'",
        (suffix, proposal_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def table_proposal(
    db: aiosqlite.Connection,
    id: str,
) -> bool:
    """Move a pending proposal to 'tabled' status. Returns True if updated."""
    cursor = await db.execute(
        "UPDATE ego_proposals SET status = 'tabled', rank = NULL, "
        "resolved_at = ? WHERE id = ? AND status = 'pending'",
        (datetime.now(UTC).isoformat(), id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def withdraw_proposal(
    db: aiosqlite.Connection,
    id: str,
) -> bool:
    """Move a pending proposal to 'withdrawn' status. Returns True if updated."""
    cursor = await db.execute(
        "UPDATE ego_proposals SET status = 'withdrawn', rank = NULL, "
        "resolved_at = ? WHERE id = ? AND status = 'pending'",
        (datetime.now(UTC).isoformat(), id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_board(
    db: aiosqlite.Connection,
    *,
    board_size: int = 3,
) -> list[dict]:
    """Active proposal board — pending proposals ordered by rank, capped.

    Returns at most ``board_size`` pending proposals, ordered by rank
    (NULLS LAST) then by creation time (newest first for unranked).
    """
    cursor = await db.execute(
        "SELECT * FROM ego_proposals WHERE status = 'pending' "
        "ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank ASC, "
        "created_at DESC LIMIT ?",
        (board_size,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_tabled(db: aiosqlite.Connection) -> list[dict]:
    """All tabled proposals, newest first."""
    cursor = await db.execute(
        "SELECT * FROM ego_proposals WHERE status = 'tabled' ORDER BY resolved_at DESC",
    )
    return [dict(r) for r in await cursor.fetchall()]


async def revoke_proposal(
    db: aiosqlite.Connection,
    id: str,
    *,
    user_response: str | None = None,
) -> bool:
    """Revoke an approved proposal (approved → rejected).

    Used during the grace period between approval and dispatch.
    Returns True if a row was updated.
    """
    cursor = await db.execute(
        "UPDATE ego_proposals SET status = 'rejected', user_response = ?, "
        "resolved_at = ? WHERE id = ? AND status = 'approved'",
        (user_response or "revoked by user", datetime.now(UTC).isoformat(), id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def expire_stale_proposals(db: aiosqlite.Connection) -> int:
    """Expire pending proposals past their expires_at.

    Also expires corresponding intervention_journal entries to keep the
    journal in sync (avoids orphaned 'pending' entries inflating ego context).

    Returns count of expired proposals.
    """
    now = datetime.now(UTC).isoformat()
    # Get IDs first so we can update journal too
    cursor = await db.execute(
        "SELECT id FROM ego_proposals "
        "WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return 0

    ids = [r[0] for r in rows]
    # Expire proposals
    await db.execute(
        "UPDATE ego_proposals SET status = 'expired', resolved_at = ? "
        "WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?",
        (now, now),
    )
    # Expire matching journal entries
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE intervention_journal SET outcome_status = 'expired', "
        f"resolved_at = ? WHERE proposal_id IN ({placeholders}) "
        f"AND outcome_status = 'pending'",
        (now, *ids),
    )
    await db.commit()
    if ids:
        logger.info("Expired %d stale proposal(s)", len(ids))
    return len(ids)


async def get_batch_for_delivery(
    db: aiosqlite.Connection,
    delivery_id: str,
) -> str | None:
    """Resolve a delivery_id to its batch_id via ego_state."""
    return await get_state(db, f"delivery_batch:{delivery_id}")


def _next_date(date_str: str) -> str:
    """Return the next day as YYYY-MM-DD."""
    from datetime import date as dt_date
    from datetime import timedelta

    d = dt_date.fromisoformat(date_str)
    return (d + timedelta(days=1)).isoformat()


async def daily_ego_cost(
    db: aiosqlite.Connection,
    *,
    date: str | None = None,
) -> float:
    """Sum cost_usd for ego cycles created on the given date.

    Parameters
    ----------
    date:
        ISO date string (YYYY-MM-DD). Defaults to today (UTC).

    Returns 0.0 if no cycles found.
    """
    if date is None:
        from datetime import UTC, datetime

        date = datetime.now(UTC).strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ego_cycles "
        "WHERE created_at >= ? || 'T00:00:00' "
        "AND created_at < ? || 'T00:00:00'",
        (date, _next_date(date)),
    ) as cur:
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0


async def daily_dispatch_cost(
    db: aiosqlite.Connection,
    *,
    date: str | None = None,
) -> float:
    """Sum cost_usd for ego-dispatched sessions created on the given date.

    Queries cc_sessions WHERE source_tag='ego_dispatch'. Returns 0.0 if
    no dispatched sessions found or if the cc_sessions table doesn't exist.
    """
    if date is None:
        date = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        async with db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM cc_sessions "
            "WHERE source_tag = 'ego_dispatch' "
            "AND started_at >= ? || 'T00:00:00' "
            "AND started_at < ? || 'T00:00:00'",
            (date, _next_date(date)),
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0
    except Exception:
        logger.warning("daily_dispatch_cost query failed", exc_info=True)
        return 0.0
