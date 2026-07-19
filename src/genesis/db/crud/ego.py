"""CRUD operations for ego_cycles, ego_proposals, and ego_state tables."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

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
    output_hash: str | None = None,
    output_size: int | None = None,
    ego_source: str = "",
    previous_hash: str | None = None,
    chain_hash: str | None = None,
) -> str:
    """Insert a new ego cycle record. Returns the id."""
    if created_at is None:
        created_at = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO ego_cycles
           (id, output_text, proposals_json, focus_summary,
            model_used, cost_usd, input_tokens, output_tokens,
            duration_ms, created_at, output_hash, output_size,
            ego_source, previous_hash, chain_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            output_hash,
            output_size,
            ego_source,
            previous_hash,
            chain_hash,
        ),
    )
    await db.commit()
    return id


async def create_cycle_chained(
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
    output_hash: str = "",
    output_size: int | None = None,
    ego_source: str = "",
) -> str:
    """Insert ego cycle with hash chain.

    Fetches the previous cycle's chain_hash, computes the new chain link,
    and inserts. Note: the read-then-insert has a theoretical TOCTOU race
    if two ego sessions (user + genesis) create cycles simultaneously.
    This is low-probability and detectable: two records sharing the same
    previous_hash indicates a fork (concurrent write), not tampering.
    verify_chain() catches it.
    """
    from genesis.ego.integrity import chained_hash

    if created_at is None:
        created_at = datetime.now(UTC).isoformat()

    cursor = await db.execute(
        "SELECT chain_hash FROM ego_cycles "
        "WHERE chain_hash IS NOT NULL "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    prev_chain = row[0] if row else None

    chain = chained_hash(output_hash, prev_chain)

    await db.execute(
        """INSERT INTO ego_cycles
           (id, output_text, proposals_json, focus_summary,
            model_used, cost_usd, input_tokens, output_tokens,
            duration_ms, created_at, output_hash, output_size,
            ego_source, previous_hash, chain_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            output_hash,
            output_size,
            ego_source,
            prev_chain,
            chain,
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
# ego_cycle_outcomes  (unified cognitive loop — Learn phase)
# ---------------------------------------------------------------------------


async def create_cycle_outcome(
    db: aiosqlite.Connection,
    *,
    cycle_id: str,
    focus_type: str,
    focus_id: str | None = None,
    num_proposals: int = 0,
    num_dispatches: int = 0,
    assessment: str | None = None,
    signals_consumed: str | None = None,
    perception_rationale: str | None = None,
    perceive_cost_usd: float = 0.0,
) -> str:
    """Record a cycle outcome for the Learn phase.

    ``assessment`` is the ego's free-text self-review of the focused goal and
    is populated ONLY on goal_review cycles (the LLM emits ``goal_assessment``
    only then). Non-goal_review cycles write NULL by design — a low populated
    fraction across all rows is expected, not a bug. Read back per-goal via
    :func:`get_latest_goal_assessment`.
    """
    from datetime import UTC, datetime

    await db.execute(
        """INSERT INTO ego_cycle_outcomes
           (cycle_id, focus_type, focus_id, num_proposals, num_dispatches,
            assessment, signals_consumed, perception_rationale,
            perceive_cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            cycle_id,
            focus_type,
            focus_id,
            num_proposals,
            num_dispatches,
            assessment,
            signals_consumed,
            perception_rationale,
            perceive_cost_usd,
            datetime.now(UTC).isoformat(),
        ),
    )
    await db.commit()
    return cycle_id


async def get_latest_goal_assessment(
    db: aiosqlite.Connection, focus_id: str,
) -> str | None:
    """Most recent non-empty goal_assessment for *focus_id*, or None.

    Surfaces the ego's own last verdict on a specific goal so a later
    goal_review cycle sees it (closes the write-only gap on
    ``ego_cycle_outcomes.assessment``).
    """
    cursor = await db.execute(
        """SELECT assessment FROM ego_cycle_outcomes
           WHERE focus_id = ? AND assessment IS NOT NULL AND assessment != ''
           ORDER BY created_at DESC LIMIT 1""",
        (focus_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def list_cycle_outcomes(
    db: aiosqlite.Connection,
    *,
    limit: int = 10,
) -> list[dict]:
    """List recent cycle outcomes (newest first)."""
    cursor = await db.execute(
        """SELECT cycle_id, focus_type, focus_id, num_proposals,
                  num_dispatches, assessment, signals_consumed,
                  perception_rationale, perceive_cost_usd, created_at
           FROM ego_cycle_outcomes
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


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


async def has_pending_cli_approval(
    db: aiosqlite.Connection, source_tag: str,
) -> bool:
    """True if an ego cycle for *source_tag* is currently blocked on a pending
    autonomous-CLI approval.

    Display-only signal for the dashboard cadence card. Matches on the approval
    request description ("Approve Claude Code session for <label>?") where
    <label> is the dispatcher's ``source_tag.replace("_", " ")`` — the same
    label the ego passes as ``action_label``.
    """
    label = source_tag.replace("_", " ")
    cursor = await db.execute(
        "SELECT 1 FROM approval_requests "
        "WHERE status = 'pending' "
        "AND action_type = 'autonomous_cli_fallback' "
        "AND description LIKE ? LIMIT 1",
        (f"%for {label}?%",),
    )
    return await cursor.fetchone() is not None


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


async def has_pending_proposal_with_hash(
    db: aiosqlite.Connection, content_hash: str,
) -> bool:
    """Check if a pending or approved proposal with this content hash exists."""
    cursor = await db.execute(
        "SELECT 1 FROM ego_proposals "
        "WHERE content_hash = ? AND status IN ('pending', 'approved') "
        "LIMIT 1",
        (content_hash,),
    )
    return await cursor.fetchone() is not None


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
    goal_id: str | None = None,
    content_hash: str | None = None,
    content_size: int | None = None,
    original_content: str | None = None,
    expected_outputs: str | None = None,
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
            ego_source, goal_id, content_hash, content_size,
            original_content, expected_outputs)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?)""",
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
            goal_id,
            content_hash,
            content_size,
            original_content,
            expected_outputs,
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
    ego_source: str | None = None,
) -> list[dict]:
    """All proposals, optionally filtered by status and/or ego_source, newest first.

    If ``ego_source`` is provided, only returns proposals from that ego.
    NULL ego_source proposals (pre-migration) match any filter.
    """
    clauses: list[str] = []
    params: list[str | int] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if ego_source:
        clauses.append("(ego_source = ? OR ego_source IS NULL)")
        params.append(ego_source)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    cursor = await db.execute(
        f"SELECT * FROM ego_proposals{where} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
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


async def claim_proposal_for_dispatch(
    db: aiosqlite.Connection,
    id: str,
) -> bool:
    """Atomically claim an approved proposal for dispatch.

    Sets status='executed' and user_response='dispatching' ONLY if the
    proposal is still in 'approved' status. The WHERE guard prevents
    double-dispatch when multiple callers (cadence, Telegram, dashboard)
    race to claim the same proposal.

    Unlike execute_proposal(), this does NOT set resolved_at — the
    original resolved_at timestamp must be preserved for the 48h
    staleness guard.

    Returns True if claimed, False if already claimed by another path.
    """
    cursor = await db.execute(
        "UPDATE ego_proposals SET status = 'executed', "
        "user_response = 'dispatching' "
        "WHERE id = ? AND status = 'approved'",
        (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def record_dispatch_session(
    db: aiosqlite.Connection,
    id: str,
    *,
    session_id: str,
) -> bool:
    """Record the actual session ID after successful dispatch spawn."""
    cursor = await db.execute(
        "UPDATE ego_proposals SET user_response = ? WHERE id = ?",
        (session_id, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def revert_failed_dispatch(
    db: aiosqlite.Connection,
    id: str,
) -> bool:
    """Revert a proposal from executed back to approved after dispatch failure.

    Only reverts if the proposal is still in 'executed' status — prevents
    overwriting a valid resolution that occurred between the failed
    dispatch and this revert.
    """
    cursor = await db.execute(
        "UPDATE ego_proposals SET status = 'approved', "
        "user_response = NULL "
        "WHERE id = ? AND status = 'executed'",
        (id,),
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
    suffix = f"|{'completed' if success else 'failed'}:{summary[:1000]}"
    cursor = await db.execute(
        "UPDATE ego_proposals SET user_response = COALESCE(user_response, '') || ? "
        "WHERE id = ? AND status = 'executed'",
        (suffix, proposal_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_proposal_verification_failed(
    db: aiosqlite.Connection,
    proposal_id: str,
    *,
    summary: str,
) -> bool:
    """Transition an executed proposal to 'failed' after verification failure.

    Called when post-dispatch output verification detects missing or
    insufficient deliverables. Distinct from :func:`update_proposal_outcome`
    which appends outcome text but keeps status as 'executed'.
    """
    suffix = f"|verification_failed:{summary[:1000]}"
    cursor = await db.execute(
        "UPDATE ego_proposals SET status = 'failed', "
        "user_response = COALESCE(user_response, '') || ? "
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


async def unboard_proposal(
    db: aiosqlite.Connection,
    id: str,
) -> bool:
    """Remove a pending proposal from the board without changing status.

    Clears ``rank`` so the proposal drops out of the ego's focus board
    but remains in the pending queue for user approval.
    Returns True if a row was updated.
    """
    cursor = await db.execute(
        "UPDATE ego_proposals SET rank = NULL "
        "WHERE id = ? AND status = 'pending'",
        (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_pending_queue(
    db: aiosqlite.Connection,
    *,
    ego_source: str | None = None,
) -> list[dict]:
    """All pending proposals — ranked first, then unranked by age.

    Unlike ``get_board`` this has no size limit and returns every pending
    proposal.  Callers can split the result into board (ranked) and queue
    (unranked) for display.
    """
    if ego_source:
        cursor = await db.execute(
            "SELECT * FROM ego_proposals "
            "WHERE status = 'pending' AND (ego_source = ? OR ego_source IS NULL) "
            "ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, "
            "rank ASC, created_at ASC",
            (ego_source,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM ego_proposals WHERE status = 'pending' "
            "ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, "
            "rank ASC, created_at ASC",
        )
    return [dict(r) for r in await cursor.fetchall()]


async def auto_table_stale_proposals(
    db: aiosqlite.Connection,
    max_age_days: int = 14,
) -> int:
    """Auto-table pending proposals older than *max_age_days*.

    Also updates corresponding intervention_journal entries to keep
    them in sync (same pattern as :func:`expire_stale_proposals`).

    Returns count of auto-tabled proposals.
    """
    now = datetime.now(UTC).isoformat()
    threshold = f"-{max_age_days} days"

    # Get IDs first so we can update journal too
    cursor = await db.execute(
        "SELECT id FROM ego_proposals "
        "WHERE status = 'pending' AND created_at < datetime('now', ?)",
        (threshold,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return 0

    ids = [r[0] for r in rows]
    # Table proposals
    await db.execute(
        "UPDATE ego_proposals SET status = 'tabled', rank = NULL, "
        "resolved_at = ? "
        "WHERE status = 'pending' AND created_at < datetime('now', ?)",
        (now, threshold),
    )
    # Update matching journal entries
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE intervention_journal SET outcome_status = 'tabled', "
        f"resolved_at = ? WHERE proposal_id IN ({placeholders}) "
        f"AND outcome_status = 'pending'",
        (now, *ids),
    )
    await db.commit()
    logger.info("Auto-tabled %d stale proposal(s) (>%dd)", len(ids), max_age_days)

    # Shorter threshold for unranked proposals — proposals that were never
    # put on the board are lower-priority and should be cleaned up faster.
    unranked_days = min(max_age_days, 5)
    unranked_threshold = f"-{unranked_days} days"
    cursor2 = await db.execute(
        "SELECT id FROM ego_proposals "
        "WHERE status = 'pending' AND rank IS NULL "
        "AND created_at < datetime('now', ?)",
        (unranked_threshold,),
    )
    unranked_rows = await cursor2.fetchall()
    unranked_count = 0
    if unranked_rows:
        unranked_ids = [r[0] for r in unranked_rows]
        await db.execute(
            "UPDATE ego_proposals SET status = 'tabled', rank = NULL, "
            "resolved_at = ? "
            "WHERE status = 'pending' AND rank IS NULL "
            "AND created_at < datetime('now', ?)",
            (now, unranked_threshold),
        )
        placeholders2 = ",".join("?" * len(unranked_ids))
        await db.execute(
            f"UPDATE intervention_journal SET outcome_status = 'tabled', "
            f"resolved_at = ? WHERE proposal_id IN ({placeholders2}) "
            f"AND outcome_status = 'pending'",
            (now, *unranked_ids),
        )
        await db.commit()
        unranked_count = len(unranked_ids)
        logger.info(
            "Auto-tabled %d unranked proposal(s) (>%dd)",
            unranked_count, unranked_days,
        )

    return len(ids) + unranked_count


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


async def get_tabled(
    db: aiosqlite.Connection,
    *,
    ego_source: str | None = None,
) -> list[dict]:
    """All tabled proposals, newest first.

    If ``ego_source`` is provided, only returns proposals from that ego.
    NULL ego_source proposals (pre-migration) match any filter.
    """
    if ego_source:
        cursor = await db.execute(
            "SELECT * FROM ego_proposals "
            "WHERE status = 'tabled' AND (ego_source = ? OR ego_source IS NULL) "
            "ORDER BY resolved_at DESC",
            (ego_source,),
        )
    else:
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


async def rolling_daily_ego_cost(
    db: aiosqlite.Connection,
    *,
    days: int = 7,
) -> float:
    """Average daily ego cycle cost over the last N days.

    Returns 0.0 if no cycles found. Used for observational display only.
    """
    from datetime import timedelta

    end_date = datetime.now(UTC).strftime("%Y-%m-%d")
    start_date = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ego_cycles "
        "WHERE created_at >= ? || 'T00:00:00' "
        "AND created_at < ? || 'T00:00:00'",
        (start_date, end_date),
    ) as cur:
        row = await cur.fetchone()
        total = float(row[0]) if row else 0.0
    return round(total / max(days, 1), 4)


# ---------------------------------------------------------------------------
# ego_directives
# ---------------------------------------------------------------------------


async def create_directive(
    db: aiosqlite.Connection,
    *,
    content: str,
    priority: str = "normal",
    ego_target: str = "user_ego",
    source: str = "user",
) -> str:
    """Insert a new ego directive. Returns the id."""
    import uuid

    directive_id = uuid.uuid4().hex[:16]
    created_at = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO ego_directives
           (id, content, priority, source, ego_target, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?)""",
        (directive_id, content, priority, source, ego_target, created_at),
    )
    await db.commit()
    return directive_id


async def list_active_directives(
    db: aiosqlite.Connection,
    ego_target: str = "user_ego",
    limit: int = 5,
    *,
    kind: str = "directive",
) -> list[dict]:
    """Active directives for a given ego target, newest first.

    ``kind`` defaults to plain directives so pre-decision callers are
    unchanged; decision rows have their own accessors below.
    """
    cursor = await db.execute(
        "SELECT * FROM ego_directives "
        "WHERE status = 'active' AND ego_target = ? AND kind = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (ego_target, kind, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def resolve_directive(
    db: aiosqlite.Connection,
    directive_id: str,
    *,
    status: str = "completed",
    resolution: str = "",
) -> bool:
    """Mark a directive as completed/cancelled. Returns True if updated.

    Decision rows are structurally excluded: the ego cannot complete or
    cancel a user ruling — only ``supersede_decision`` (a user action)
    retires one.
    """
    resolved_at = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE ego_directives SET status = ?, resolved_at = ?, resolution = ? "
        "WHERE id = ? AND status = 'active' AND kind != 'decision'",
        (status, resolved_at, resolution, directive_id),
    )
    await db.commit()
    return cursor.rowcount > 0


# ── Decision rows (kind='decision') — durable user rulings ──────────────


async def create_decision(
    db: aiosqlite.Connection,
    *,
    content: str,
    ego_target: str = "user_ego",
    source_proposal_id: str | None = None,
    source: str = "user",
) -> str:
    """Insert a decision row. Returns the id."""
    import uuid

    decision_id = uuid.uuid4().hex[:16]
    created_at = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO ego_directives
           (id, content, priority, source, ego_target, status, created_at,
            kind, source_proposal_id)
           VALUES (?, ?, 'high', ?, ?, 'active', ?, 'decision', ?)""",
        (decision_id, content, source, ego_target, created_at, source_proposal_id),
    )
    await db.commit()
    return decision_id


async def list_active_decisions(
    db: aiosqlite.Connection,
    ego_target: str = "user_ego",
    limit: int = 7,
) -> tuple[list[dict], int]:
    """(rows, total_active) — most recently affirmed first, capped."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM ego_directives "
        "WHERE status = 'active' AND kind = 'decision' AND ego_target = ?",
        (ego_target,),
    )
    row = await cursor.fetchone()
    total = int(row[0]) if row else 0
    cursor = await db.execute(
        "SELECT * FROM ego_directives "
        "WHERE status = 'active' AND kind = 'decision' AND ego_target = ? "
        "ORDER BY COALESCE(last_reaffirmed_at, created_at) DESC LIMIT ?",
        (ego_target, limit),
    )
    return [dict(r) for r in await cursor.fetchall()], total


async def find_active_decision(
    db: aiosqlite.Connection,
    *,
    prefix: str,
    ego_target: str = "user_ego",
) -> dict | None:
    """Active decision whose content starts with the ``[type/category]``
    dedup prefix, or None."""
    cursor = await db.execute(
        "SELECT * FROM ego_directives "
        "WHERE status = 'active' AND kind = 'decision' AND ego_target = ? "
        "AND content LIKE ? ORDER BY created_at DESC LIMIT 1",
        (ego_target, prefix + "%"),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def reaffirm_decision(db: aiosqlite.Connection, decision_id: str) -> bool:
    """Record a repeat ruling on an existing decision."""
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE ego_directives SET reaffirm_count = reaffirm_count + 1, "
        "last_reaffirmed_at = ? WHERE id = ? AND status = 'active' "
        "AND kind = 'decision'",
        (now, decision_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def supersede_decision(
    db: aiosqlite.Connection,
    decision_id: str,
    *,
    resolution: str = "",
) -> bool:
    """Retire a decision — the ONLY path that does, and it is user-driven.

    Uses status 'cancelled' with a 'superseded: …' resolution (the status
    CHECK predates decisions; a rebuild for a fourth enum value isn't
    worth it — kind + resolution disambiguate).
    """
    resolved_at = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE ego_directives SET status = 'cancelled', resolved_at = ?, "
        "resolution = ? WHERE id = ? AND status = 'active' AND kind = 'decision'",
        (resolved_at, f"superseded: {resolution}" if resolution else "superseded",
         decision_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_goal_proposal_summary(
    db: aiosqlite.Connection,
    goal_id: str,
) -> dict[str, int]:
    """Count proposals by status for a given goal_id."""
    try:
        cursor = await db.execute(
            "SELECT status, count(*) FROM ego_proposals "
            "WHERE goal_id = ? GROUP BY status",
            (goal_id,),
        )
        return dict(await cursor.fetchall())
    except Exception:
        return {}


async def has_open_goal_status_change(
    db: aiosqlite.Connection,
    goal_id: str,
) -> bool:
    """True if an unresolved ``goal_status_change`` proposal exists for *goal_id*.

    Used by goal-review post-processing to avoid stacking a second status-change
    proposal on a goal while one is still pending or awaiting dispatch-approval.
    """
    cursor = await db.execute(
        "SELECT 1 FROM ego_proposals "
        "WHERE goal_id = ? AND action_type = 'goal_status_change' "
        "AND status IN ('pending', 'approved') LIMIT 1",
        (goal_id,),
    )
    return await cursor.fetchone() is not None


async def compute_vcr(
    db: aiosqlite.Connection,
    *,
    days: int = 30,
) -> dict:
    """Compute Verified Completion Rate for ego proposals.

    VCR measures whether autonomously dispatched proposals actually
    succeeded, not just whether they were dispatched.

    Returns dict with:
      - total_resolved: all proposals that left 'pending' state
      - total_executed: proposals that were dispatched as sessions
      - outcomes_completed: executed proposals whose session succeeded
      - outcomes_failed: executed proposals whose session failed
      - outcomes_unknown: executed proposals with no outcome data
      - vcr: outcomes_completed / total_executed (0.0 if none)
      - dispatch_rate: total_executed / total_resolved (0.0 if none)
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        cursor = await db.execute(
            "SELECT status, user_response FROM ego_proposals "
            "WHERE resolved_at >= ? OR (status = 'executed' AND created_at >= ?)",
            (since, since),
        )
        rows = await cursor.fetchall()
    except Exception:
        return {
            "total_resolved": 0, "total_executed": 0,
            "outcomes_completed": 0, "outcomes_failed": 0,
            "outcomes_unknown": 0, "vcr": 0.0, "dispatch_rate": 0.0,
        }

    total_resolved = 0
    total_executed = 0
    completed = 0
    failed = 0
    unknown = 0

    for status, user_response in rows:
        if status in ("pending",):
            continue  # not yet resolved
        total_resolved += 1
        resp = user_response or ""
        if status == "executed":
            total_executed += 1
            if "|completed:" in resp:
                completed += 1
            elif "|failed:" in resp:
                failed += 1
            else:
                unknown += 1
        elif status == "failed" and "|verification_failed:" in resp:
            # Verification-failed proposals were dispatched (reached
            # 'executed' before verification flipped them to 'failed').
            # Count them as dispatch failures for VCR accuracy.
            total_executed += 1
            failed += 1

    vcr = completed / total_executed if total_executed > 0 else 0.0
    dispatch_rate = total_executed / total_resolved if total_resolved > 0 else 0.0

    return {
        "total_resolved": total_resolved,
        "total_executed": total_executed,
        "outcomes_completed": completed,
        "outcomes_failed": failed,
        "outcomes_unknown": unknown,
        "vcr": round(vcr, 4),
        "dispatch_rate": round(dispatch_rate, 4),
    }


# ---------------------------------------------------------------------------
# ego_proposals — j9 eval analytical reads (read-only, time-windowed)
# ---------------------------------------------------------------------------


async def get_proposals_for_drift(
    db: aiosqlite.Connection,
    *,
    start: str,
    end: str,
) -> list[dict]:
    """Proposals in ``[start, end)`` for cognitive-drift metrics.

    Returns ``action_type``, ``alternatives`` and ``realist_verdict`` per row —
    the inputs to the j9 anti-overfitting baseline (dissent / alternative /
    diversity rates).
    """
    cursor = await db.execute(
        """SELECT action_type, alternatives, realist_verdict
           FROM ego_proposals
           WHERE created_at >= ? AND created_at < ?""",
        (start, end),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_acceptance_counts(
    db: aiosqlite.Connection,
    *,
    start: str,
    end: str,
) -> dict:
    """Resolved-proposal acceptance counts in ``[start, end)``.

    Excludes ``pending``/``expired``.  Returns ``{"total", "accepted"}`` where
    ``accepted`` counts ``approved``/``executed`` proposals — the ego
    acceptance-rate inputs for the j9 system composite.  ``accepted`` is
    ``None`` when no proposals match (SUM over zero rows).
    """
    cursor = await db.execute(
        """SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'approved' OR status = 'executed' THEN 1 ELSE 0 END) as accepted
        FROM ego_proposals
        WHERE created_at >= ? AND created_at < ?
        AND status NOT IN ('pending', 'expired')""",
        (start, end),
    )
    row = await cursor.fetchone()
    return dict(row) if row else {"total": 0, "accepted": 0}


async def get_proposals_for_quality(
    db: aiosqlite.Connection,
    *,
    start: str,
    end: str,
) -> list[dict]:
    """Proposals in ``[start, end)`` for ego-quality metrics.

    Returns ``id``, ``status``, ``confidence`` and ``action_type`` per row —
    the inputs to approval-rate, execution-success and confidence-calibration.
    """
    cursor = await db.execute(
        """SELECT id, status, confidence, action_type
        FROM ego_proposals
        WHERE created_at >= ? AND created_at < ?""",
        (start, end),
    )
    return [dict(r) for r in await cursor.fetchall()]
