"""CRUD operations for user_model_cache table."""

from __future__ import annotations

import json

import aiosqlite


async def get_current(db: aiosqlite.Connection) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM user_model_cache WHERE id = 'current'"
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_person(db: aiosqlite.Connection, person_id: str) -> dict | None:
    """Get user model for a specific person."""
    cursor = await db.execute(
        "SELECT * FROM user_model_cache WHERE person_id = ?", (person_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert(
    db: aiosqlite.Connection,
    *,
    model_json: dict,
    synthesized_at: str,
    synthesized_by: str,
    person_id: str | None = None,
    evidence_count: int = 0,
    last_change_type: str | None = None,
) -> str:
    row_id = person_id if person_id else "current"
    existing = (
        await get_by_person(db, person_id)
        if person_id
        else await get_current(db)
    )
    if existing:
        new_version = existing["version"] + 1
        await db.execute(
            """UPDATE user_model_cache SET
               model_json = ?, version = ?, synthesized_at = ?, synthesized_by = ?,
               evidence_count = ?, last_change_type = ?, last_changed_at = ?
               WHERE id = ?""",
            (json.dumps(model_json), new_version, synthesized_at, synthesized_by,
             evidence_count, last_change_type,
             synthesized_at if last_change_type else existing.get("last_changed_at"),
             row_id),
        )
    else:
        await db.execute(
            """INSERT INTO user_model_cache
               (id, person_id, model_json, version, synthesized_at, synthesized_by,
                evidence_count, last_change_type, last_changed_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (row_id, person_id, json.dumps(model_json), synthesized_at, synthesized_by,
             evidence_count, last_change_type,
             synthesized_at if last_change_type else None),
        )
    await db.commit()
    return row_id


async def delete(db: aiosqlite.Connection, person_id: str | None = None) -> bool:
    row_id = person_id if person_id else "current"
    cursor = await db.execute(
        "DELETE FROM user_model_cache WHERE id = ?", (row_id,)
    )
    await db.commit()
    return cursor.rowcount > 0
