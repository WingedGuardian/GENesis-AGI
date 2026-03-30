"""CRUD operations for tool_registry table."""

from __future__ import annotations

import json

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    name: str,
    category: str,
    description: str,
    tool_type: str,
    created_at: str,
    provider: str | None = None,
    cost_tier: str | None = None,
    metadata: dict | None = None,
) -> str:
    await db.execute(
        """INSERT INTO tool_registry
           (id, name, category, description, tool_type, provider,
            cost_tier, created_at, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, name, category, description, tool_type, provider,
         cost_tier, created_at, json.dumps(metadata) if metadata else None),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    name: str,
    category: str,
    description: str,
    tool_type: str,
    created_at: str,
    provider: str | None = None,
    cost_tier: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO tool_registry
           (id, name, category, description, tool_type, provider,
            cost_tier, created_at, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             name = excluded.name, category = excluded.category,
             description = excluded.description, tool_type = excluded.tool_type,
             provider = excluded.provider, cost_tier = excluded.cost_tier,
             metadata = excluded.metadata""",
        (id, name, category, description, tool_type, provider,
         cost_tier, created_at, json.dumps(metadata) if metadata else None),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM tool_registry WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_by_category(
    db: aiosqlite.Connection, category: str, *, limit: int = 50
) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM tool_registry WHERE category = ? ORDER BY name LIMIT ?",
        (category, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM tool_registry ORDER BY category, name"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def record_invocation(db: aiosqlite.Connection, id: str, *, last_used: str) -> bool:
    cursor = await db.execute(
        "UPDATE tool_registry SET usage_count = usage_count + 1, last_used_at = ? WHERE id = ?",
        (last_used, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update(db: aiosqlite.Connection, id: str, **fields) -> bool:
    if not fields:
        return False
    if "metadata" in fields and isinstance(fields["metadata"], dict):
        fields["metadata"] = json.dumps(fields["metadata"])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [id]
    cursor = await db.execute(
        f"UPDATE tool_registry SET {set_clause} WHERE id = ?", values
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM tool_registry WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
