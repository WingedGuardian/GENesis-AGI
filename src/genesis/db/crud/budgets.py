"""CRUD operations for budgets table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    budget_type: str,
    limit_usd: float,
    created_at: str,
    updated_at: str,
    person_id: str | None = None,
    scope: str | None = None,
    warning_pct: float = 0.80,
    active: int = 1,
) -> str:
    await db.execute(
        """INSERT INTO budgets
           (id, budget_type, person_id, scope, limit_usd, warning_pct,
            active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, budget_type, person_id, scope, limit_usd, warning_pct,
         active, created_at, updated_at),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    budget_type: str,
    limit_usd: float,
    created_at: str,
    updated_at: str,
    person_id: str | None = None,
    scope: str | None = None,
    warning_pct: float = 0.80,
    active: int = 1,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO budgets
           (id, budget_type, person_id, scope, limit_usd, warning_pct,
            active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             budget_type = excluded.budget_type, person_id = excluded.person_id,
             scope = excluded.scope, limit_usd = excluded.limit_usd,
             warning_pct = excluded.warning_pct, active = excluded.active,
             updated_at = excluded.updated_at""",
        (id, budget_type, person_id, scope, limit_usd, warning_pct,
         active, created_at, updated_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM budgets WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_active(
    db: aiosqlite.Connection,
    *,
    budget_type: str | None = None,
    person_id: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM budgets WHERE active = 1"
    params: list = []
    if budget_type is not None:
        sql += " AND budget_type = ?"
        params.append(budget_type)
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += " ORDER BY created_at DESC"
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def update_limit(
    db: aiosqlite.Connection, id: str, *, limit_usd: float, updated_at: str
) -> bool:
    cursor = await db.execute(
        "UPDATE budgets SET limit_usd = ?, updated_at = ? WHERE id = ?",
        (limit_usd, updated_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def deactivate(
    db: aiosqlite.Connection, id: str, *, updated_at: str
) -> bool:
    cursor = await db.execute(
        "UPDATE budgets SET active = 0, updated_at = ? WHERE id = ?",
        (updated_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM budgets WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
