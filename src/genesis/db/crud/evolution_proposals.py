"""CRUD operations for evolution_proposals table."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    proposal_type: str,
    current_content: str,
    proposed_change: str,
    rationale: str,
    source_reflection_id: str | None = None,
) -> str:
    """Create an evolution proposal. Returns proposal id."""
    proposal_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO evolution_proposals
           (id, proposal_type, current_content, proposed_change, rationale,
            source_reflection_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (proposal_id, proposal_type, current_content, proposed_change,
         rationale, source_reflection_id, now_iso),
    )
    await db.commit()
    return proposal_id


async def get(db: aiosqlite.Connection, proposal_id: str) -> dict | None:
    """Get a proposal by id."""
    cursor = await db.execute(
        "SELECT * FROM evolution_proposals WHERE id = ?", (proposal_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row, strict=False))


async def update_status(
    db: aiosqlite.Connection,
    proposal_id: str,
    status: str,
) -> bool:
    """Update proposal status. Returns True if row existed."""
    now_iso = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE evolution_proposals SET status = ?, reviewed_at = ? WHERE id = ?",
        (status, now_iso, proposal_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_pending(
    db: aiosqlite.Connection,
    *,
    limit: int = 10,
    since: str | None = None,
    proposal_type: str | None = None,
) -> list[dict]:
    """List pending proposals, newest first.

    Optional filters (both backward-compatible — None = no filter):
    - ``since``: ISO-8601 timestamp; only proposals created at or after.
    - ``proposal_type``: restrict to a single proposal_type value.
    """
    sql = "SELECT * FROM evolution_proposals WHERE status = 'pending'"
    params: list = []
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(since)
    if proposal_type is not None:
        sql += " AND proposal_type = ?"
        params.append(proposal_type)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in rows]
