"""CRUD operations for execution_traces table."""

from __future__ import annotations

import json

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    user_request: str,
    plan: list,
    sub_agents: list,
    created_at: str,
    person_id: str | None = None,
    initiated_by: str = "user",
    total_cost_usd: float = 0.0,
) -> str:
    await db.execute(
        """INSERT INTO execution_traces
           (id, person_id, initiated_by, user_request, plan, sub_agents,
            total_cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, person_id, initiated_by, user_request, json.dumps(plan),
         json.dumps(sub_agents), total_cost_usd, created_at),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    user_request: str,
    plan: list,
    sub_agents: list,
    created_at: str,
    person_id: str | None = None,
    initiated_by: str = "user",
    total_cost_usd: float = 0.0,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO execution_traces
           (id, person_id, initiated_by, user_request, plan, sub_agents,
            total_cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             person_id = excluded.person_id,
             initiated_by = excluded.initiated_by,
             user_request = excluded.user_request, plan = excluded.plan,
             sub_agents = excluded.sub_agents, total_cost_usd = excluded.total_cost_usd""",
        (id, person_id, initiated_by, user_request, json.dumps(plan),
         json.dumps(sub_agents), total_cost_usd, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM execution_traces WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_by_outcome(
    db: aiosqlite.Connection,
    outcome_class: str,
    *,
    person_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM execution_traces WHERE outcome_class = ?"
    params: list = [outcome_class]
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def complete(
    db: aiosqlite.Connection,
    id: str,
    *,
    outcome_class: str,
    completed_at: str,
    quality_gate: dict | None = None,
    total_cost_usd: float | None = None,
    request_delivery_delta: dict | None = None,
) -> bool:
    fields = {"outcome_class": outcome_class, "completed_at": completed_at}
    if quality_gate is not None:
        fields["quality_gate"] = json.dumps(quality_gate)
    if total_cost_usd is not None:
        fields["total_cost_usd"] = total_cost_usd
    if request_delivery_delta is not None:
        fields["request_delivery_delta"] = json.dumps(request_delivery_delta)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [id]
    cursor = await db.execute(
        f"UPDATE execution_traces SET {set_clause} WHERE id = ?", values
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM execution_traces WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
