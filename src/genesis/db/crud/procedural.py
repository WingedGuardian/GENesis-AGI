"""CRUD operations for procedural_memory table."""

from __future__ import annotations

import json

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    created_at: str,
    person_id: str | None = None,
    speculative: int = 1,
    attempted_workarounds: list | None = None,
    version: int = 1,
    activation_tier: str = "L4",
    tool_trigger: list[str] | None = None,
    success_count: int = 0,
    confidence: float = 0.0,
) -> str:
    await db.execute(
        """INSERT INTO procedural_memory
           (id, person_id, task_type, principle, steps, tools_used, context_tags,
            speculative, attempted_workarounds, version, created_at,
            activation_tier, tool_trigger, success_count, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, person_id, task_type, principle, json.dumps(steps), json.dumps(tools_used),
         json.dumps(context_tags), speculative,
         json.dumps(attempted_workarounds) if attempted_workarounds else None,
         version, created_at, activation_tier,
         json.dumps(tool_trigger) if tool_trigger else None,
         success_count, confidence),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    task_type: str,
    principle: str,
    steps: list[str],
    tools_used: list[str],
    context_tags: list[str],
    created_at: str,
    person_id: str | None = None,
    speculative: int = 1,
    attempted_workarounds: list | None = None,
    version: int = 1,
    activation_tier: str = "L4",
    tool_trigger: list[str] | None = None,
    success_count: int = 0,
    confidence: float = 0.0,
) -> str:
    """Idempotent write: insert or update on conflict.

    Mirrors `create()`'s `success_count` / `confidence` plumbing so that
    callers building idempotent seed/teach paths (e.g., batch seed scripts,
    future explicit-teach upserters) can't accidentally land rows with the
    invisible-to-recall (speculative=0, success_count=0, confidence=0.0)
    profile that bit `procedure_store` before this fix.
    """
    await db.execute(
        """INSERT INTO procedural_memory
           (id, person_id, task_type, principle, steps, tools_used, context_tags,
            speculative, attempted_workarounds, version, created_at,
            activation_tier, tool_trigger, success_count, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             person_id = excluded.person_id,
             task_type = excluded.task_type, principle = excluded.principle,
             steps = excluded.steps, tools_used = excluded.tools_used,
             context_tags = excluded.context_tags, speculative = excluded.speculative,
             attempted_workarounds = excluded.attempted_workarounds,
             version = excluded.version,
             activation_tier = excluded.activation_tier,
             tool_trigger = excluded.tool_trigger,
             success_count = excluded.success_count,
             confidence = excluded.confidence""",
        (id, person_id, task_type, principle, json.dumps(steps), json.dumps(tools_used),
         json.dumps(context_tags), speculative,
         json.dumps(attempted_workarounds) if attempted_workarounds else None,
         version, created_at, activation_tier,
         json.dumps(tool_trigger) if tool_trigger else None,
         success_count, confidence),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM procedural_memory WHERE id = ?", (id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def list_by_task_type(
    db: aiosqlite.Connection,
    task_type: str,
    *,
    person_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM procedural_memory WHERE task_type = ?"
    params: list = [task_type]
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += " ORDER BY confidence DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_tier(
    db: aiosqlite.Connection,
    tier: str,
    *,
    limit: int = 100,
) -> list[dict]:
    """List active procedures at a specific activation tier."""
    cursor = await db.execute(
        """SELECT * FROM procedural_memory
           WHERE activation_tier = ? AND deprecated = 0 AND quarantined = 0
           ORDER BY confidence DESC LIMIT ?""",
        (tier, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_tool_trigger(
    db: aiosqlite.Connection,
    tool_name: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """List active procedures whose tool_trigger JSON array contains tool_name."""
    # SQLite JSON: json_each extracts array elements for matching
    cursor = await db.execute(
        """SELECT p.* FROM procedural_memory p, json_each(p.tool_trigger) j
           WHERE j.value = ? AND p.deprecated = 0 AND p.quarantined = 0
           ORDER BY p.confidence DESC LIMIT ?""",
        (tool_name, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def update(db: aiosqlite.Connection, id: str, **fields) -> bool:
    if not fields:
        return False
    # Serialize JSON fields
    for key in ("steps", "tools_used", "context_tags", "failure_modes",
                 "attempted_workarounds", "tool_trigger"):
        if key in fields and isinstance(fields[key], list):
            fields[key] = json.dumps(fields[key])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [id]
    cursor = await db.execute(
        f"UPDATE procedural_memory SET {set_clause} WHERE id = ?", values
    )
    await db.commit()
    return cursor.rowcount > 0


async def quarantine(db: aiosqlite.Connection, id: str) -> bool:
    """Mark a procedure as quarantined (excluded from matching)."""
    cursor = await db.execute(
        "UPDATE procedural_memory SET quarantined = 1 WHERE id = ?", (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def unquarantine(db: aiosqlite.Connection, id: str) -> bool:
    """Remove quarantine flag from a procedure."""
    cursor = await db.execute(
        "UPDATE procedural_memory SET quarantined = 0 WHERE id = ?", (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_active(
    db: aiosqlite.Connection,
    *,
    person_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List non-deprecated, non-quarantined procedures."""
    sql = "SELECT * FROM procedural_memory WHERE deprecated = 0 AND quarantined = 0"
    params: list = []
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += " ORDER BY confidence DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def list_quarantined(db: aiosqlite.Connection) -> list[dict]:
    """List all quarantined procedures."""
    cursor = await db.execute(
        "SELECT * FROM procedural_memory WHERE quarantined = 1"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM procedural_memory WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
