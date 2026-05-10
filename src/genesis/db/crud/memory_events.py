"""CRUD operations for memory_events table (SVO event calendar)."""

from __future__ import annotations

import uuid

import aiosqlite


async def insert(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    subject: str,
    verb: str,
    object_: str | None = None,
    event_date: str | None = None,
    event_date_end: str | None = None,
    confidence: float = 0.5,
    source_session_id: str | None = None,
    _commit: bool = True,
) -> str:
    """Insert a memory event. Returns the event ID.

    Set ``_commit=False`` when called inside a batch loop where the caller
    manages transaction boundaries (e.g., extraction_job).
    """
    event_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO memory_events "
        "(id, memory_id, subject, verb, object, event_date, event_date_end, "
        "confidence, source_session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id, memory_id, subject, verb, object_,
            event_date, event_date_end, confidence, source_session_id,
        ),
    )
    if _commit:
        await db.commit()
    return event_id


async def query_by_date_range(
    db: aiosqlite.Connection,
    start: str,
    end: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Query events within a date range (inclusive)."""
    cursor = await db.execute(
        "SELECT * FROM memory_events "
        "WHERE event_date >= ? AND event_date <= ? "
        "ORDER BY event_date DESC LIMIT ?",
        (start, end, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def query_by_subject(
    db: aiosqlite.Connection,
    subject: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Query events by subject (case-insensitive prefix match)."""
    cursor = await db.execute(
        "SELECT * FROM memory_events "
        "WHERE subject LIKE ? "
        "ORDER BY event_date DESC LIMIT ?",
        (f"{subject}%", limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def query_timeline(
    db: aiosqlite.Connection,
    *,
    subject: str | None = None,
    verb: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query events as a timeline, optionally filtered by subject/verb."""
    conditions: list[str] = []
    params: list[str | int] = []

    if subject:
        conditions.append("subject LIKE ?")
        params.append(f"{subject}%")
    if verb:
        conditions.append("verb = ?")
        params.append(verb)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    cursor = await db.execute(
        f"SELECT * FROM memory_events {where} "  # noqa: S608
        "ORDER BY event_date DESC LIMIT ?",
        params,
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_memory_ids_in_range(
    db: aiosqlite.Connection,
    start: str,
    end: str,
    *,
    limit: int = 50,
) -> list[str]:
    """Return distinct memory_ids for events in a date range.

    Used by the retrieval pipeline to boost temporally relevant memories.
    """
    cursor = await db.execute(
        "SELECT DISTINCT memory_id FROM memory_events "
        "WHERE event_date >= ? AND event_date <= ? "
        "ORDER BY event_date DESC LIMIT ?",
        (start, end, limit),
    )
    return [row[0] for row in await cursor.fetchall()]
