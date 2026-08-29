"""CRUD operations for the intervention journal.

Tracks ego proposals from creation through resolution, recording
expected vs actual outcomes for metacognitive feedback.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)


async def create(
    db: aiosqlite.Connection,
    *,
    ego_source: str,
    proposal_id: str,
    cycle_id: str,
    action_type: str,
    action_summary: str,
    expected_outcome: str = "",
    confidence: float = 0.0,
    created_at: str | None = None,
) -> str:
    """Create a journal entry when a proposal is born."""
    jid = uuid.uuid4().hex[:16]
    created_at = created_at or datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO intervention_journal
           (id, ego_source, proposal_id, cycle_id, action_type,
            action_summary, expected_outcome, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (jid, ego_source, proposal_id, cycle_id, action_type,
         action_summary, expected_outcome, confidence, created_at),
    )
    await db.commit()
    return jid


async def resolve(
    db: aiosqlite.Connection,
    proposal_id: str,
    *,
    outcome_status: str,
    actual_outcome: str | None = None,
    user_response: str | None = None,
    resolved_at: str | None = None,
) -> bool:
    """Update a pending journal entry when its proposal resolves.

    Returns True if a row was updated, False if no pending entry found.
    """
    resolved_at = resolved_at or datetime.now(UTC).isoformat()
    cur = await db.execute(
        """UPDATE intervention_journal
           SET outcome_status = ?, actual_outcome = ?,
               user_response = ?, resolved_at = ?
           WHERE proposal_id = ? AND outcome_status = 'pending'""",
        (outcome_status, actual_outcome, user_response,
         resolved_at, proposal_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_by_proposal(
    db: aiosqlite.Connection,
    proposal_id: str,
) -> dict | None:
    """Fetch the journal entry for a given proposal."""
    cur = await db.execute(
        "SELECT * FROM intervention_journal WHERE proposal_id = ?",
        (proposal_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))


async def recent_resolved(
    db: aiosqlite.Connection,
    *,
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """Return recently resolved entries for ego context display."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cur = await db.execute(
        """SELECT action_type, action_summary, expected_outcome,
                  actual_outcome, outcome_status, user_response,
                  confidence, resolved_at
           FROM intervention_journal
           WHERE outcome_status != 'pending' AND resolved_at >= ?
           ORDER BY resolved_at DESC
           LIMIT ?""",
        (cutoff, limit),
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def unresolved_count(db: aiosqlite.Connection) -> int:
    """Count pending (unresolved) journal entries."""
    cur = await db.execute(
        "SELECT COUNT(*) FROM intervention_journal WHERE outcome_status = 'pending'"
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def aggregate_by_type(
    db: aiosqlite.Connection,
    *,
    exclude_proposals_within_days: int | None = None,
) -> list[dict]:
    """Aggregate outcomes by action_type for capability map input.

    Returns rows with: action_type, total, approved, rejected, executed,
    failed, avg_confidence.

    ``exclude_proposals_within_days`` omits rows whose proposal is ALSO counted
    by a caller reading ``ego_proposals`` over the same window. Creating a
    proposal batch writes an ego_proposals row and a journal row for each
    proposal, so a caller aggregating both tables counts one observation twice;
    passing its own window here makes the journal contribute only the history
    the other source cannot see. Defaults to None — no exclusion — so callers
    that read this table alone are unaffected.
    """
    if exclude_proposals_within_days is not None and exclude_proposals_within_days < 0:
        # A negative value renders the modifier '--N days', which SQLite
        # REJECTS -> NULL. `p.created_at >= NULL` is then NULL rather than
        # false, so NOT EXISTS holds for every row and the de-duplication
        # silently becomes a no-op -- every proposal counted twice again,
        # with a result that looks perfectly healthy. Measured, not assumed:
        # datetime('now','--5 days') is NULL and '<any> >= NULL' is NULL.
        # `capability_map._recency_clause` already refuses this loudly; the
        # two windowed APIs must not disagree about it.
        raise ValueError(
            "exclude_proposals_within_days must be >= 0 or None, got "
            f"{exclude_proposals_within_days}"
        )
    cur = await db.execute(
        """SELECT
               action_type,
               COUNT(*) as total,
               SUM(CASE WHEN outcome_status = 'approved' THEN 1 ELSE 0 END) as approved,
               SUM(CASE WHEN outcome_status = 'rejected' THEN 1 ELSE 0 END) as rejected,
               SUM(CASE WHEN outcome_status = 'executed' THEN 1 ELSE 0 END) as executed,
               SUM(CASE WHEN outcome_status = 'failed' THEN 1 ELSE 0 END) as failed,
               ROUND(AVG(confidence), 2) as avg_confidence
           FROM intervention_journal
           WHERE outcome_status != 'pending'"""
        + (
            # NOT EXISTS, not NOT IN. `x NOT IN (…)` evaluates to NULL --
            # never true -- as soon as the subquery yields a single NULL, so one
            # NULL id in ego_proposals would discard the ENTIRE resolved journal
            # history rather than the duplicate rows, silently shrinking every
            # domain's sample size. `id` is a TEXT PRIMARY KEY, which SQLite
            # does not constrain to NOT NULL, so that is reachable in an
            # imported or repaired database. The correlated form is NULL-safe by
            # construction and also handles a NULL proposal_id without the extra
            # OR: `p.id = NULL` is never true, so the row is kept.
            """
             AND NOT EXISTS (
                   SELECT 1 FROM ego_proposals p
                   WHERE p.id = intervention_journal.proposal_id
                     AND p.created_at >= datetime('now', ?)
                     AND p.status IN ('approved','executed','rejected','failed'))"""
            if exclude_proposals_within_days is not None
            else ""
        )
        + """
           GROUP BY action_type
           ORDER BY total DESC""",
        ()
        if exclude_proposals_within_days is None
        else (f"-{int(exclude_proposals_within_days)} days",),
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]
