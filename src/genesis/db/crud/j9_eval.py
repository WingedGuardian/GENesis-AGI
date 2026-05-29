"""CRUD operations for J-9 eval infrastructure (eval_events + eval_snapshots)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


# ── eval_events ──────────────────────────────────────────────────────────────


async def insert_event(
    db: aiosqlite.Connection,
    *,
    dimension: str,
    event_type: str,
    metrics: dict,
    subject_id: str | None = None,
    session_id: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Append an eval event. Returns the event id."""
    eid = _new_id()
    ts = timestamp or _now_iso()
    await db.execute(
        """INSERT INTO eval_events
           (id, timestamp, dimension, event_type, subject_id,
            session_id, metrics_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (eid, ts, dimension, event_type, subject_id,
         session_id, json.dumps(metrics), ts),
    )
    await db.commit()
    return eid


async def get_events(
    db: aiosqlite.Connection,
    *,
    dimension: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    session_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Query eval events with optional filters."""
    sql = "SELECT * FROM eval_events WHERE 1=1"
    params: list = []
    if dimension:
        sql += " AND dimension = ?"
        params.append(dimension)
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    if since:
        sql += " AND timestamp >= ?"
        params.append(since)
    if until:
        sql += " AND timestamp < ?"
        params.append(until)
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("metrics_json"):
            d["metrics"] = json.loads(d["metrics_json"])
        result.append(d)
    return result


async def count_events(
    db: aiosqlite.Connection,
    *,
    dimension: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
) -> int:
    """Count eval events with optional filters."""
    sql = "SELECT COUNT(*) as cnt FROM eval_events WHERE 1=1"
    params: list = []
    if dimension:
        sql += " AND dimension = ?"
        params.append(dimension)
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    if since:
        sql += " AND timestamp >= ?"
        params.append(since)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return row["cnt"] if row else 0


# ── eval_snapshots ───────────────────────────────────────────────────────────


async def insert_snapshot(
    db: aiosqlite.Connection,
    *,
    period_start: str,
    period_end: str,
    period_type: str,
    dimension: str,
    metrics: dict,
    sample_count: int,
) -> str:
    """Insert a periodic aggregation snapshot. Returns the snapshot id."""
    sid = _new_id()
    await db.execute(
        """INSERT INTO eval_snapshots
           (id, period_start, period_end, period_type, dimension,
            metrics_json, sample_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, period_start, period_end, period_type, dimension,
         json.dumps(metrics), sample_count, _now_iso()),
    )
    await db.commit()
    return sid


async def get_snapshots(
    db: aiosqlite.Connection,
    *,
    dimension: str | None = None,
    period_type: str | None = None,
    since: str | None = None,
    limit: int = 52,
) -> list[dict]:
    """Query snapshots, most recent first."""
    sql = "SELECT * FROM eval_snapshots WHERE 1=1"
    params: list = []
    if dimension:
        sql += " AND dimension = ?"
        params.append(dimension)
    if period_type:
        sql += " AND period_type = ?"
        params.append(period_type)
    if since:
        sql += " AND period_end >= ?"
        params.append(since)
    sql += " ORDER BY period_end DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("metrics_json"):
            d["metrics"] = json.loads(d["metrics_json"])
        result.append(d)
    return result


async def get_latest_snapshot(
    db: aiosqlite.Connection,
    *,
    dimension: str,
    period_type: str = "weekly",
) -> dict | None:
    """Get the most recent snapshot for a dimension."""
    cursor = await db.execute(
        """SELECT * FROM eval_snapshots
           WHERE dimension = ? AND period_type = ?
           ORDER BY period_end DESC LIMIT 1""",
        (dimension, period_type),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("metrics_json"):
        d["metrics"] = json.loads(d["metrics_json"])
    return d


# ── eval_subsystem_grades ───────────────────────────────────────────────────


async def insert_subsystem_grade(
    db: aiosqlite.Connection,
    *,
    period_start: str,
    period_end: str,
    period_type: str,
    subsystem: str,
    grade: str | None,
    score: float | None,
    factors: dict,
    sample_count: int,
) -> str:
    """Insert or update a per-subsystem quality grade. Returns the grade id.

    Uses deterministic ID from (subsystem, period_end, period_type) to
    prevent duplicate rows from repeated aggregation runs.
    """
    import hashlib
    gid = hashlib.sha256(
        f"{subsystem}:{period_end}:{period_type}".encode(),
    ).hexdigest()[:16]
    await db.execute(
        """INSERT INTO eval_subsystem_grades
           (id, period_start, period_end, period_type, subsystem,
            grade, score, factors_json, sample_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
            grade = excluded.grade,
            score = excluded.score,
            factors_json = excluded.factors_json,
            sample_count = excluded.sample_count,
            created_at = excluded.created_at""",
        (gid, period_start, period_end, period_type, subsystem,
         grade, score, json.dumps(factors), sample_count, _now_iso()),
    )
    await db.commit()
    return gid


async def get_subsystem_grades(
    db: aiosqlite.Connection,
    *,
    subsystem: str | None = None,
    period_type: str | None = None,
    limit: int = 52,
) -> list[dict]:
    """Query subsystem grades, most recent first."""
    sql = "SELECT * FROM eval_subsystem_grades WHERE 1=1"
    params: list = []
    if subsystem:
        sql += " AND subsystem = ?"
        params.append(subsystem)
    if period_type:
        sql += " AND period_type = ?"
        params.append(period_type)
    sql += " ORDER BY period_end DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("factors_json"):
            d["factors"] = json.loads(d["factors_json"])
        result.append(d)
    return result


async def get_latest_subsystem_grades(
    db: aiosqlite.Connection,
    period_type: str = "weekly",
) -> list[dict]:
    """Get the most recent grade for each subsystem."""
    cursor = await db.execute(
        """SELECT g.* FROM eval_subsystem_grades g
           INNER JOIN (
               SELECT subsystem, MAX(period_end) as max_end
               FROM eval_subsystem_grades
               WHERE period_type = ?
               GROUP BY subsystem
           ) latest ON g.subsystem = latest.subsystem
                    AND g.period_end = latest.max_end
           WHERE g.period_type = ?""",
        (period_type, period_type),
    )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("factors_json"):
            d["factors"] = json.loads(d["factors_json"])
        result.append(d)
    return result
