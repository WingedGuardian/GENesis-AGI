"""CRUD operations for reflection_corpus table.

Records prompt I/O pairs from every reflection dispatch for quality
measurement and future DSPy optimization.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)


async def record(
    db: aiosqlite.Connection,
    *,
    depth: str,
    prompt_text: str,
    response_text: str,
    tick_id: str | None = None,
    focus_area: str | None = None,
    model_used: str | None = None,
    parsed_ok: bool | None = None,
) -> str | None:
    """Record a prompt/response pair.  Returns the row ID or None on error."""
    row_id = str(uuid.uuid4())
    try:
        await db.execute(
            """INSERT INTO reflection_corpus
               (id, depth, focus_area, prompt_text, response_text,
                parsed_ok, model_used, tick_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                depth,
                focus_area,
                prompt_text,
                response_text,
                int(parsed_ok) if parsed_ok is not None else None,
                model_used,
                tick_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()
        return row_id
    except Exception:
        logger.debug("reflection_corpus record failed", exc_info=True)
        return None


async def mark_parsed(
    db: aiosqlite.Connection, row_id: str, *, parsed_ok: bool,
) -> None:
    """Update the parsed_ok flag after output parsing completes."""
    try:
        await db.execute(
            "UPDATE reflection_corpus SET parsed_ok = ? WHERE id = ?",
            (int(parsed_ok), row_id),
        )
        await db.commit()
    except Exception:
        logger.debug("reflection_corpus mark_parsed failed", exc_info=True)


async def count(
    db: aiosqlite.Connection,
    *,
    depth: str | None = None,
    quality_label: str | None = None,
) -> int:
    """Count corpus entries, optionally filtered."""
    sql = "SELECT COUNT(*) FROM reflection_corpus WHERE 1=1"
    params: list = []
    if depth is not None:
        sql += " AND depth = ?"
        params.append(depth)
    if quality_label is not None:
        sql += " AND quality_label = ?"
        params.append(quality_label)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return row[0] if row else 0


async def tick_written_before(
    db: aiosqlite.Connection,
    depth: str,
    at: str,
    *,
    window_seconds: float = 60.0,
    exclude_tick_id: str | None = None,
) -> str | None:
    """The tick_id of the one ``depth`` corpus row written at or up to
    ``window_seconds`` before ``at`` (an ISO UTC timestamp).

    A reflection writes its corpus row just before the summary observation it
    produces (0.00-0.26 s apart on the newest 10 Light runs, measured
    2026-11-11), so this finds the run behind a given observation. A row
    written after the observation belongs to a later run (one whose output
    failed to parse writes a row and no observation) and is never picked.
    Returns None when no row is in the window or ``at`` does not parse, and
    when more than one run has a row there: two overlapping reflections can
    write their rows in either order around one's output, so which run is
    behind ``at`` cannot be told (no two Light rows fell within 120 s of each
    other in 517, measured 2026-11-12).
    """
    try:
        upper = datetime.fromisoformat(at).astimezone(UTC)
    except (TypeError, ValueError):
        return None
    lower = upper - timedelta(seconds=window_seconds)
    fmt = "%Y-%m-%dT%H:%M:%S.%f+00:00"
    sql = (
        "SELECT DISTINCT tick_id FROM reflection_corpus WHERE depth = ? "
        "AND created_at <= ? AND created_at >= ? AND tick_id IS NOT NULL"
    )
    params: list = [depth, upper.strftime(fmt), lower.strftime(fmt)]
    if exclude_tick_id is not None:
        sql += " AND tick_id != ?"
        params.append(exclude_tick_id)
    sql += " LIMIT 2"
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return rows[0][0] if len(rows) == 1 else None
