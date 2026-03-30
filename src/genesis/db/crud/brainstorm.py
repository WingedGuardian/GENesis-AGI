"""CRUD operations for brainstorm_log table."""

from __future__ import annotations

import json

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    session_type: str,
    model_used: str,
    outputs: list,
    created_at: str,
    staging_ids: list | None = None,
    journal_entry_ref: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO brainstorm_log
           (id, session_type, model_used, outputs, staging_ids, journal_entry_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (id, session_type, model_used, json.dumps(outputs),
         json.dumps(staging_ids) if staging_ids else None,
         journal_entry_ref, created_at),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    session_type: str,
    model_used: str,
    outputs: list,
    created_at: str,
    staging_ids: list | None = None,
    journal_entry_ref: str | None = None,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO brainstorm_log
           (id, session_type, model_used, outputs, staging_ids, journal_entry_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             session_type = excluded.session_type, model_used = excluded.model_used,
             outputs = excluded.outputs, staging_ids = excluded.staging_ids,
             journal_entry_ref = excluded.journal_entry_ref""",
        (id, session_type, model_used, json.dumps(outputs),
         json.dumps(staging_ids) if staging_ids else None,
         journal_entry_ref, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM brainstorm_log WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_by_type(
    db: aiosqlite.Connection, session_type: str, *, limit: int = 20
) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM brainstorm_log WHERE session_type = ? ORDER BY created_at DESC LIMIT ?",
        (session_type, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def update_counts(
    db: aiosqlite.Connection, id: str, *, promoted_count: int, discarded_count: int
) -> bool:
    cursor = await db.execute(
        "UPDATE brainstorm_log SET promoted_count = ?, discarded_count = ? WHERE id = ?",
        (promoted_count, discarded_count, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM brainstorm_log WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
