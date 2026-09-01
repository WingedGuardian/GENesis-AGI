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
    origin: str = "ego",
    proposal_id: str | None = None,
) -> str | None:
    """Create a new intention. Returns id, or None if cap reached.

    ``origin='ego'`` (LLM-created) rows count against MAX_ACTIVE_PER_SOURCE;
    ``origin='system'`` rows (mechanical dispatch follow-through) bypass the
    cap — it exists to stop the LLM hoarding slots, not to drop follow-through.
    ``proposal_id`` at creation is the system row's source dispatch proposal
    (dedup key); ``fire()`` COALESCE-preserves it — a fire without a new
    proposal_id keeps this creation-time value rather than nulling it.
    """
    if origin not in ("ego", "system"):
        origin = "ego"
    if origin == "ego":
        count = await count_active(db, ego_source, origin="ego")
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
            created_at, cycle_count, max_cycles, reasoning, priority,
            origin, proposal_id)
           VALUES (?, ?, ?, ?, 'active', ?, 0, ?, ?, ?, ?, ?)""",
        (intention_id, content, trigger_condition, ego_source,
         _now_iso(), max_cycles, reasoning, priority, origin, proposal_id),
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
    *,
    origin: str | None = None,
) -> int:
    """Count active intentions for an ego source.

    ``origin`` filters to one origin class ('ego' | 'system'); None counts all.
    """
    if origin:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM ego_intentions "
            "WHERE ego_source = ? AND status = 'active' AND origin = ?",
            (ego_source, origin),
        )
    else:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM ego_intentions "
            "WHERE ego_source = ? AND status = 'active'",
            (ego_source,),
        )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def has_active_for_proposal(
    db: aiosqlite.Connection,
    ego_source: str,
    proposal_id: str,
) -> bool:
    """True if an ACTIVE intention already references this proposal (dedup)."""
    cursor = await db.execute(
        "SELECT 1 FROM ego_intentions "
        "WHERE ego_source = ? AND status = 'active' AND proposal_id = ? "
        "LIMIT 1",
        (ego_source, proposal_id),
    )
    return await cursor.fetchone() is not None


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
        cursor = await db.execute(
            "UPDATE ego_intentions SET cycle_count = cycle_count + 1 "
            "WHERE id = ? AND status = 'active' AND ego_source = ?",
            (intention_id, ego_source),
        )
    else:
        cursor = await db.execute(
            "UPDATE ego_intentions SET cycle_count = cycle_count + 1 "
            "WHERE id = ? AND status = 'active'",
            (intention_id,),
        )
    if cursor.rowcount == 0:
        return 0  # No row matched — wrong source or not active
    result = await db.execute(
        "SELECT cycle_count FROM ego_intentions WHERE id = ?",
        (intention_id,),
    )
    row = await result.fetchone()
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
    cross-ego isolation. COALESCE keeps a system row's creation-time
    provenance (source dispatch proposal) when fired without a new
    proposal_id.
    """
    if ego_source:
        cursor = await db.execute(
            "UPDATE ego_intentions SET status = 'fired', fired_at = ?, "
            "proposal_id = COALESCE(?, proposal_id) "
            "WHERE id = ? AND status = 'active' AND ego_source = ?",
            (_now_iso(), proposal_id, intention_id, ego_source),
        )
    else:
        cursor = await db.execute(
            "UPDATE ego_intentions SET status = 'fired', fired_at = ?, "
            "proposal_id = COALESCE(?, proposal_id) "
            "WHERE id = ? AND status = 'active'",
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


async def increment_unreviewed(
    db: aiosqlite.Connection,
    ego_source: str,
    reviewed_ids: list[str],
) -> int:
    """Implicit-keep: bump cycle_count on active rows NOT in reviewed_ids.

    Makes ``max_cycles`` a MECHANICAL TTL — a row the ego's review output
    omits (or a cycle whose output drops the intentions block entirely)
    still ages, so ``expire_overdue`` can reap it even under LLM
    non-compliance. Returns rows bumped. Caller should commit.
    """
    ids = [i for i in reviewed_ids if i]
    query = (
        "UPDATE ego_intentions SET cycle_count = cycle_count + 1 "
        "WHERE ego_source = ? AND status = 'active'"
    )
    params: list[str] = [ego_source]
    if ids:
        placeholders = ",".join("?" * len(ids))
        query += f" AND id NOT IN ({placeholders})"
        params.extend(ids)
    cursor = await db.execute(query, params)
    return cursor.rowcount


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
