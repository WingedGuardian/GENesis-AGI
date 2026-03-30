"""CRUD operations for speculative_claims table."""

from __future__ import annotations

import json

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    claim: str,
    hypothesis_expiry: str,
    created_at: str,
    source_reflection_id: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO speculative_claims
           (id, claim, hypothesis_expiry, source_reflection_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (id, claim, hypothesis_expiry, source_reflection_id, created_at),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    claim: str,
    hypothesis_expiry: str,
    created_at: str,
    source_reflection_id: str | None = None,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO speculative_claims
           (id, claim, hypothesis_expiry, source_reflection_id, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             claim = excluded.claim, hypothesis_expiry = excluded.hypothesis_expiry,
             source_reflection_id = excluded.source_reflection_id""",
        (id, claim, hypothesis_expiry, source_reflection_id, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM speculative_claims WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_active(db: aiosqlite.Connection, *, limit: int = 50) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM speculative_claims WHERE speculative = 1 AND archived_at IS NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def add_evidence(
    db: aiosqlite.Connection, id: str, *, memory_id: str
) -> bool:
    row = await get_by_id(db, id)
    if not row:
        return False
    confirmed_by = json.loads(row["confirmed_by"]) if row["confirmed_by"] else []
    confirmed_by.append(memory_id)
    new_count = row["evidence_count"] + 1
    # Confirm at 3+ evidence points
    speculative = 0 if new_count >= 3 else 1
    cursor = await db.execute(
        "UPDATE speculative_claims SET evidence_count = ?, confirmed_by = ?, speculative = ? "
        "WHERE id = ?",
        (new_count, json.dumps(confirmed_by), speculative, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def archive(db: aiosqlite.Connection, id: str, *, archived_at: str) -> bool:
    cursor = await db.execute(
        "UPDATE speculative_claims SET archived_at = ? WHERE id = ?",
        (archived_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM speculative_claims WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
