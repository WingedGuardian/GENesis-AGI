"""CRUD for ``tool_call_outcomes``.

Two writers share the table, deduplicated by the unique ``tool_use_id``:

* the CC hook script ``scripts/edit_failure_sensor.py`` (Edit and Write only,
  raw sqlite3 by necessity, since it runs outside the server), and
* the transcript extraction job, through :func:`insert_outcomes`, for every
  main-session tool call (``learning/tool_outcomes.py``).

``INSERT OR IGNORE`` means whichever writer sees a call first wins; both decide
failure from the result's own error flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

# Retention (pruned daily by ``runtime/init/learning._wire_drip_retention_jobs``).
# Rows: 180 days, the same horizon as ``calibration_cell_history``; the per-tool
# calibration cells' "all-time" window is therefore at most this long. Failure
# text: 90 days, the longest calibration window, so scrubbed error text is not
# kept indefinitely while the outcome itself still counts.
ROW_RETENTION_DAYS = 180
SNIPPET_RETENTION_DAYS = 90


async def prune(
    db: aiosqlite.Connection,
    *,
    now: datetime | None = None,
    row_days: int = ROW_RETENTION_DAYS,
    snippet_days: int = SNIPPET_RETENTION_DAYS,
) -> tuple[int, int]:
    """Delete rows past ``row_days`` and clear failure text past ``snippet_days``.

    Returns ``(rows_deleted, snippets_cleared)``. Timestamps are written in two
    ISO spellings (``Z`` from transcripts, ``+00:00`` from ``isoformat``). A plain
    string comparison would differ from the instant comparison only within the
    same second, which is immaterial at day-scale retention; ``datetime()`` is
    used so the rule reads as what it means.
    """
    now = now or datetime.now(UTC)
    row_cut = (now - timedelta(days=row_days)).isoformat()
    text_cut = (now - timedelta(days=snippet_days)).isoformat()
    deleted = await db.execute(
        "DELETE FROM tool_call_outcomes WHERE datetime(timestamp) < datetime(?)",
        (row_cut,),
    )
    cleared = await db.execute(
        "UPDATE tool_call_outcomes SET error_snippet = NULL "
        "WHERE error_snippet IS NOT NULL AND datetime(timestamp) < datetime(?)",
        (text_cut,),
    )
    await db.commit()
    return deleted.rowcount, cleared.rowcount  # type: ignore[return-value]


async def insert_outcomes(db: aiosqlite.Connection, rows: list[tuple]) -> None:
    """Insert outcome rows, ignoring any ``tool_use_id`` already recorded.

    Each row is ``(session_id, tool_name, file_path, success, error_snippet,
    timestamp, tool_use_id, error_class)``.
    """
    if not rows:
        return
    await db.executemany(
        "INSERT OR IGNORE INTO tool_call_outcomes "
        "(session_id, tool_name, file_path, success, error_snippet, timestamp, tool_use_id,"
        " error_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()


async def aggregate_success_rates(
    db: aiosqlite.Connection, *, since: str | None = None
) -> list[dict]:
    """Per-tool call counts and success base rates, optionally windowed.

    ``since`` is a canonical-ISO floor compared sargably against the stored
    timestamps (both sides come from ``datetime.isoformat()``; a
    microsecond-less row sorts sub-second early at the boundary — negligible
    on day-scale windows). Rides ``idx_tco_tool_ts``.
    """
    if since is None:
        cursor = await db.execute(
            "SELECT tool_name, COUNT(*) AS n, AVG(success) AS base_rate "
            "FROM tool_call_outcomes GROUP BY tool_name"
        )
    else:
        cursor = await db.execute(
            "SELECT tool_name, COUNT(*) AS n, AVG(success) AS base_rate "
            "FROM tool_call_outcomes WHERE timestamp >= ? GROUP BY tool_name",
            (since,),
        )
    return [dict(r) for r in await cursor.fetchall()]
