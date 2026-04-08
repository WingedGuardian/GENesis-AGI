"""CRUD operations for cost_events table."""

from __future__ import annotations

import json

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    event_type: str,
    cost_usd: float,
    created_at: str,
    model: str | None = None,
    provider: str | None = None,
    engine: str | None = None,
    task_id: str | None = None,
    person_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_known: bool = True,
    metadata: dict | None = None,
) -> str:
    await db.execute(
        """INSERT INTO cost_events
           (id, event_type, model, provider, engine, task_id, person_id,
            input_tokens, output_tokens, cost_usd, cost_known, metadata, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, event_type, model, provider, engine, task_id, person_id,
         input_tokens, output_tokens, cost_usd, int(cost_known),
         json.dumps(metadata) if metadata else None, created_at),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    event_type: str,
    cost_usd: float,
    created_at: str,
    model: str | None = None,
    provider: str | None = None,
    engine: str | None = None,
    task_id: str | None = None,
    person_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_known: bool = True,
    metadata: dict | None = None,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO cost_events
           (id, event_type, model, provider, engine, task_id, person_id,
            input_tokens, output_tokens, cost_usd, cost_known, metadata, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             event_type = excluded.event_type, model = excluded.model,
             provider = excluded.provider, engine = excluded.engine,
             task_id = excluded.task_id, person_id = excluded.person_id,
             input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens,
             cost_usd = excluded.cost_usd, cost_known = excluded.cost_known,
             metadata = excluded.metadata""",
        (id, event_type, model, provider, engine, task_id, person_id,
         input_tokens, output_tokens, cost_usd, int(cost_known),
         json.dumps(metadata) if metadata else None, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM cost_events WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def query(
    db: aiosqlite.Connection,
    *,
    task_id: str | None = None,
    person_id: str | None = None,
    event_type: str | None = None,
    engine: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM cost_events WHERE 1=1"
    params: list = []
    if task_id is not None:
        sql += " AND task_id = ?"
        params.append(task_id)
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    if event_type is not None:
        sql += " AND event_type = ?"
        params.append(event_type)
    if engine is not None:
        sql += " AND engine = ?"
        params.append(engine)
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(since)
    if until is not None:
        sql += " AND created_at < ?"
        params.append(until)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def sum_cost(
    db: aiosqlite.Connection,
    *,
    task_id: str | None = None,
    person_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    engine: str | None = None,
) -> float:
    """Aggregate cost_usd matching filters. Core of budget checking."""
    sql = "SELECT COALESCE(SUM(cost_usd), 0.0) FROM cost_events WHERE 1=1"
    params: list = []
    if task_id is not None:
        sql += " AND task_id = ?"
        params.append(task_id)
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(since)
    if until is not None:
        sql += " AND created_at < ?"
        params.append(until)
    if engine is not None:
        sql += " AND engine = ?"
        params.append(engine)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return float(row[0])


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM cost_events WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
