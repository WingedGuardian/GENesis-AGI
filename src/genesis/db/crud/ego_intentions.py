"""CRUD operations for the ego_intentions table — deferred proposal staging."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)

MAX_ACTIVE_PER_SOURCE = 5


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def create(
    db: aiosqlite.Connection,
    *,
    content: str,
    trigger_condition: str,
    ego_source: str,
    reasoning: str = "",
    priority: str = "normal",
    max_cycles: int = 20,
) -> str | None:
    """Create a new intention. Returns id, or None if cap reached."""
    count = await count_active(db, ego_source)
    if count >= MAX_ACTIVE_PER_SOURCE:
        logger.warning(
            "Intention cap reached for %s (%d/%d) — rejecting",
            ego_source, count, MAX_ACTIVE_PER_SOURCE,
        )
        return None

    intention_id = uuid.uuid4().hex[:16]
    await db.execute(
        """INSERT INTO ego_intentions
           (id, content, trigger_condition, ego_source, status,
            created_at, cycle_count, max_cycles, reasoning, priority)
           VALUES (?, ?, ?, ?, 'active', ?, 0, ?, ?, ?)""",
        (intention_id, content, trigger_condition, ego_source,
         _now_iso(), max_cycles, reasoning, priority),
    )
    # Caller should commit (batch with other operations).
    return intention_id


async def list_active(
    db: aiosqlite.Connection,
    ego_source: str,
) -> list[dict]:
    """All active intentions for an ego source, oldest first."""
    cursor = await db.execute(
        "SELECT * FROM ego_intentions "
        "WHERE ego_source = ? AND status = 'active' "
        "ORDER BY created_at ASC",
        (ego_source,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def count_active(
    db: aiosqlite.Connection,
    ego_source: str,
) -> int:
    """Count active intentions for an ego source."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM ego_intentions "
        "WHERE ego_source = ? AND status = 'active'",
        (ego_source,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def increment_cycle_count(
    db: aiosqlite.Connection,
    intention_id: str,
    *,
    ego_source: str | None = None,
) -> int:
    """Increment cycle_count, return new value. Caller should batch commits.

    If ego_source is provided, only operates on intentions belonging to
    that source (cross-ego isolation).
    """
    if ego_source:
        await db.execute(
            "UPDATE ego_intentions SET cycle_count = cycle_count + 1 "
            "WHERE id = ? AND status = 'active' AND ego_source = ?",
            (intention_id, ego_source),
        )
    else:
        await db.execute(
            "UPDATE ego_intentions SET cycle_count = cycle_count + 1 "
            "WHERE id = ? AND status = 'active'",
            (intention_id,),
        )
    cursor = await db.execute(
        "SELECT cycle_count FROM ego_intentions WHERE id = ?",
        (intention_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def fire(
    db: aiosqlite.Connection,
    intention_id: str,
    *,
    proposal_id: str | None = None,
    ego_source: str | None = None,
) -> bool:
    """Mark an intention as fired. Returns True if updated.

    Caller should commit. If ego_source is provided, enforces
    cross-ego isolation.
    """
    if ego_source:
        cursor = await db.execute(
            "UPDATE ego_intentions SET status = 'fired', fired_at = ?, "
            "proposal_id = ? WHERE id = ? AND status = 'active' AND ego_source = ?",
            (_now_iso(), proposal_id, intention_id, ego_source),
        )
    else:
        cursor = await db.execute(
            "UPDATE ego_intentions SET status = 'fired', fired_at = ?, "
            "proposal_id = ? WHERE id = ? AND status = 'active'",
            (_now_iso(), proposal_id, intention_id),
        )
    return cursor.rowcount > 0


async def withdraw(
    db: aiosqlite.Connection,
    intention_id: str,
    *,
    ego_source: str | None = None,
) -> bool:
    """Mark an intention as withdrawn. Returns True if updated.

    Caller should commit. If ego_source is provided, enforces
    cross-ego isolation.
    """
    if ego_source:
        cursor = await db.execute(
            "UPDATE ego_intentions SET status = 'withdrawn' "
            "WHERE id = ? AND status = 'active' AND ego_source = ?",
            (intention_id, ego_source),
        )
    else:
        cursor = await db.execute(
            "UPDATE ego_intentions SET status = 'withdrawn' "
            "WHERE id = ? AND status = 'active'",
            (intention_id,),
        )
    return cursor.rowcount > 0


async def renew(
    db: aiosqlite.Connection,
    intention_id: str,
    *,
    ego_source: str | None = None,
) -> bool:
    """Reset cycle_count to 0 (intention still relevant, trigger not yet met).

    Returns True if updated. Caller should commit. If ego_source is
    provided, enforces cross-ego isolation.
    """
    if ego_source:
        cursor = await db.execute(
            "UPDATE ego_intentions SET cycle_count = 0 "
            "WHERE id = ? AND status = 'active' AND ego_source = ?",
            (intention_id, ego_source),
        )
    else:
        cursor = await db.execute(
            "UPDATE ego_intentions SET cycle_count = 0 "
            "WHERE id = ? AND status = 'active'",
            (intention_id,),
        )
    return cursor.rowcount > 0


async def expire_overdue(
    db: aiosqlite.Connection,
    ego_source: str,
) -> int:
    """Auto-expire intentions past their max_cycles. Returns count expired.

    Caller should commit.
    """
    cursor = await db.execute(
        "UPDATE ego_intentions SET status = 'expired' "
        "WHERE ego_source = ? AND status = 'active' "
        "AND cycle_count > max_cycles",
        (ego_source,),
    )
    return cursor.rowcount
