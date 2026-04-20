"""CRUD operations for knowledge_uploads table (dashboard file upload tracking)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite


async def insert(
    db: aiosqlite.Connection,
    *,
    filename: str,
    file_path: str,
    file_size: int,
    mime_type: str | None = None,
    id: str | None = None,
) -> str:
    """Create an upload record in 'uploaded' state. Returns upload id."""
    upload_id = id or str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()

    await db.execute(
        """INSERT INTO knowledge_uploads
           (id, filename, file_path, file_size, mime_type, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'uploaded', ?)""",
        (upload_id, filename, file_path, file_size, mime_type, now_iso),
    )
    await db.commit()
    return upload_id


async def get(db: aiosqlite.Connection, upload_id: str) -> dict | None:
    """Get an upload record by id."""
    cursor = await db.execute(
        "SELECT * FROM knowledge_uploads WHERE id = ?", (upload_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row, strict=False))


async def update_status(
    db: aiosqlite.Connection,
    upload_id: str,
    *,
    status: str,
    project_type: str | None = None,
    domain: str | None = None,
    purpose: str | None = None,
    error_message: str | None = None,
    unit_ids: list[str] | None = None,
) -> bool:
    """Update an upload record's status and optional fields."""
    sets: list[str] = ["status = ?"]
    params: list = [status]

    if project_type is not None:
        sets.append("project_type = ?")
        params.append(project_type)
    if domain is not None:
        sets.append("domain = ?")
        params.append(domain)
    if purpose is not None:
        sets.append("purpose = ?")
        params.append(purpose)
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message)
    if unit_ids is not None:
        sets.append("unit_ids = ?")
        params.append(json.dumps(unit_ids))
    if status in ("completed", "failed"):
        sets.append("completed_at = ?")
        params.append(datetime.now(UTC).isoformat())

    params.append(upload_id)
    cursor = await db.execute(
        f"UPDATE knowledge_uploads SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_recent(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[dict]:
    """List recent uploads ordered by creation time (newest first)."""
    cursor = await db.execute(
        "SELECT * FROM knowledge_uploads ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in rows]


async def taxonomy(db: aiosqlite.Connection) -> dict:
    """Return distinct project_type and domain values for autocomplete."""
    cursor = await db.execute(
        "SELECT DISTINCT project_type FROM knowledge_units WHERE project_type IS NOT NULL",
    )
    projects = [r[0] for r in await cursor.fetchall()]

    cursor = await db.execute(
        "SELECT DISTINCT domain FROM knowledge_units WHERE domain IS NOT NULL",
    )
    domains = [r[0] for r in await cursor.fetchall()]

    return {"project_types": projects, "domains": domains}


async def atomic_transition(
    db: aiosqlite.Connection,
    upload_id: str,
    *,
    from_status: str,
    to_status: str,
    project_type: str | None = None,
    domain: str | None = None,
    purpose: str | None = None,
) -> bool:
    """Atomically transition status if current state matches from_status.

    Returns True if the transition succeeded (row was in expected state).
    Prevents TOCTOU races on concurrent /ingest requests.
    """
    sets = ["status = ?"]
    params: list = [to_status]
    if project_type is not None:
        sets.append("project_type = ?")
        params.append(project_type)
    if domain is not None:
        sets.append("domain = ?")
        params.append(domain)
    if purpose is not None:
        sets.append("purpose = ?")
        params.append(purpose)

    params.extend([upload_id, from_status])
    cursor = await db.execute(
        f"UPDATE knowledge_uploads SET {', '.join(sets)} WHERE id = ? AND status = ?",
        params,
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_chunk_progress(
    db: aiosqlite.Connection,
    upload_id: str,
    *,
    chunks_total: int | None = None,
) -> None:
    """Increment chunk completion count for a processing upload.

    Called once per chunk from parallel callbacks — uses atomic increment
    so completion order doesn't matter. Commits immediately; must not be
    called during a _commit=False batch on the same connection.
    """
    if chunks_total is not None:
        await db.execute(
            """UPDATE knowledge_uploads
               SET chunks_done = COALESCE(chunks_done, 0) + 1,
                   chunks_total = ?
               WHERE id = ?""",
            (chunks_total, upload_id),
        )
    else:
        await db.execute(
            """UPDATE knowledge_uploads
               SET chunks_done = COALESCE(chunks_done, 0) + 1
               WHERE id = ?""",
            (upload_id,),
        )
    await db.commit()


async def list_processing_before(
    db: aiosqlite.Connection,
    cutoff_iso: str,
) -> list[dict]:
    """List uploads stuck in 'processing' since before *cutoff_iso*."""
    cursor = await db.execute(
        "SELECT * FROM knowledge_uploads WHERE status = 'processing' AND created_at < ?",
        (cutoff_iso,),
    )
    rows = await cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in rows]


async def delete(db: aiosqlite.Connection, upload_id: str) -> bool:
    """Delete an upload record."""
    cursor = await db.execute(
        "DELETE FROM knowledge_uploads WHERE id = ?", (upload_id,),
    )
    await db.commit()
    return cursor.rowcount > 0
