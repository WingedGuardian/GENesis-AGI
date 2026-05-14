"""CRUD operations for the persistent events table."""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime

import aiosqlite


async def insert(
    db: aiosqlite.Connection,
    *,
    subsystem: str,
    severity: str,
    event_type: str,
    message: str,
    details: dict | None = None,
    session_id: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Insert a single event and return its ID."""
    event_id = str(uuid.uuid4())
    ts = timestamp or datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO events
           (id, timestamp, subsystem, severity, event_type, message, details,
            session_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            ts,
            subsystem,
            severity,
            event_type,
            message,
            json.dumps(details) if details else None,
            session_id,
            ts,
        ),
    )
    await db.commit()
    return event_id


async def insert_batch(
    db: aiosqlite.Connection,
    events: list[dict],
) -> int:
    """Insert a batch of events. Returns count inserted."""
    rows = []
    for e in events:
        rows.append((
            e.get("id") or str(uuid.uuid4()),
            e.get("timestamp") or datetime.now(UTC).isoformat(),
            e["subsystem"],
            e["severity"],
            e["event_type"],
            e["message"],
            json.dumps(e["details"]) if e.get("details") else None,
            e.get("session_id"),
            e.get("timestamp") or datetime.now(UTC).isoformat(),
        ))
    await db.executemany(
        """INSERT OR IGNORE INTO events
           (id, timestamp, subsystem, severity, event_type, message, details,
            session_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await db.commit()
    return len(rows)


async def query(
    db: aiosqlite.Connection,
    *,
    subsystem: str | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    session_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Query events with flexible filtering."""
    clauses: list[str] = []
    params: list = []

    if subsystem:
        clauses.append("subsystem = ?")
        params.append(subsystem)
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)
    if search:
        clauses.append("message LIKE ?")
        params.append(f"%{search}%")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    db.row_factory = aiosqlite.Row
    rows = await db.execute_fetchall(
        f"SELECT * FROM events{where} ORDER BY timestamp DESC LIMIT ?",
        params,
    )
    results = []
    for row in rows:
        d = dict(row)
        if d.get("details"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                d["details"] = json.loads(d["details"])
        results.append(d)
    return results


async def prune(
    db: aiosqlite.Connection,
    *,
    older_than: str,
    event_type: str | None = None,
) -> int:
    """Delete events older than the given ISO timestamp. Returns count deleted.

    If *event_type* is provided, only events of that type are pruned.
    """
    if event_type is not None:
        cursor = await db.execute(
            "DELETE FROM events WHERE event_type = ? AND timestamp < ?",
            (event_type, older_than),
        )
    else:
        cursor = await db.execute(
            "DELETE FROM events WHERE timestamp < ?",
            (older_than,),
        )
    await db.commit()
    return cursor.rowcount


async def count(
    db: aiosqlite.Connection,
    *,
    subsystem: str | None = None,
    severity: str | None = None,
    since: str | None = None,
) -> int:
    """Count events matching filters."""
    clauses: list[str] = []
    params: list = []

    if subsystem:
        clauses.append("subsystem = ?")
        params.append(subsystem)
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    row = await db.execute_fetchall(
        f"SELECT COUNT(*) as cnt FROM events{where}",
        params,
    )
    return row[0][0] if row else 0


# ── Severity ordering helper ────────────────────────────────────────────

_SEVERITY_ORDER = ["debug", "info", "warning", "error", "critical"]


def _severities_at_or_above(min_sev: str) -> list[str]:
    """Return list of severity strings at or above *min_sev*."""
    try:
        idx = _SEVERITY_ORDER.index(min_sev.lower())
    except ValueError:
        return _SEVERITY_ORDER  # unknown → return all
    return _SEVERITY_ORDER[idx:]


def _build_common_filters(
    *,
    min_severity: str | None = None,
    subsystems: list[str] | None = None,
    event_types: list[str] | None = None,
    search: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[list[str], list]:
    """Build WHERE clauses and params shared by paginated/count queries."""
    clauses: list[str] = []
    params: list = []

    if min_severity:
        sevs = _severities_at_or_above(min_severity)
        placeholders = ", ".join("?" for _ in sevs)
        clauses.append(f"severity IN ({placeholders})")
        params.extend(sevs)
    if subsystems:
        placeholders = ", ".join("?" for _ in subsystems)
        clauses.append(f"subsystem IN ({placeholders})")
        params.extend(subsystems)
    if event_types:
        placeholders = ", ".join("?" for _ in event_types)
        clauses.append(f"event_type IN ({placeholders})")
        params.extend(event_types)
    if search:
        clauses.append("message LIKE ?")
        params.append(f"%{search}%")
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)

    return clauses, params


async def query_paginated(
    db: aiosqlite.Connection,
    *,
    cursor_ts: str | None = None,
    cursor_id: str | None = None,
    page_size: int = 100,
    min_severity: str | None = None,
    subsystems: list[str] | None = None,
    event_types: list[str] | None = None,
    search: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[list[dict], bool]:
    """Cursor-based paginated query. Returns (events, has_more)."""
    clauses, params = _build_common_filters(
        min_severity=min_severity,
        subsystems=subsystems,
        event_types=event_types,
        search=search,
        since=since,
        until=until,
    )

    if cursor_ts and cursor_id:
        clauses.append("(timestamp < ? OR (timestamp = ? AND id < ?))")
        params.extend([cursor_ts, cursor_ts, cursor_id])

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    # Fetch one extra to detect has_more
    fetch_limit = page_size + 1
    params.append(fetch_limit)

    db.row_factory = aiosqlite.Row
    rows = await db.execute_fetchall(
        f"SELECT * FROM events{where} ORDER BY timestamp DESC, id DESC LIMIT ?",
        params,
    )

    has_more = len(rows) > page_size
    results = []
    for row in rows[:page_size]:
        d = dict(row)
        if d.get("details"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                d["details"] = json.loads(d["details"])
        results.append(d)
    return results, has_more


async def count_filtered(
    db: aiosqlite.Connection,
    *,
    min_severity: str | None = None,
    subsystems: list[str] | None = None,
    event_types: list[str] | None = None,
    search: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Count events matching the same filter set as query_paginated."""
    clauses, params = _build_common_filters(
        min_severity=min_severity,
        subsystems=subsystems,
        event_types=event_types,
        search=search,
        since=since,
        until=until,
    )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    row = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM events{where}",
        params,
    )
    return row[0][0] if row else 0


async def query_grouped_errors(
    db: aiosqlite.Connection,
    *,
    since: str | None = None,
    subsystem: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Group WARNING+ events by subsystem/event_type/message prefix."""
    clauses: list[str] = []
    params: list = []

    sevs = _severities_at_or_above("warning")
    placeholders = ", ".join("?" for _ in sevs)
    clauses.append(f"severity IN ({placeholders})")
    params.extend(sevs)

    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if subsystem:
        clauses.append("subsystem = ?")
        params.append(subsystem)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    db.row_factory = aiosqlite.Row
    rows = await db.execute_fetchall(
        f"""SELECT subsystem, event_type,
                   SUBSTR(message, 1, 80) AS msg_prefix,
                   MAX(severity) AS worst_severity,
                   COUNT(*) AS count,
                   MIN(timestamp) AS first_seen,
                   MAX(timestamp) AS last_seen
            FROM events{where}
            GROUP BY subsystem, event_type, msg_prefix
            ORDER BY last_seen DESC
            LIMIT ?""",
        params,
    )
    return [dict(r) for r in rows]
