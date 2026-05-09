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


async def aggregate_by_type(db: aiosqlite.Connection) -> list[dict]:
    """Aggregate outcomes by action_type for capability map input.

    Returns rows with: action_type, total, approved, rejected, executed,
    failed, avg_confidence.
    """
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
           WHERE outcome_status != 'pending'
           GROUP BY action_type
           ORDER BY total DESC"""
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]
