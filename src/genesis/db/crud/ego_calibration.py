"""CRUD for ``ego_calibration_snapshots`` — measure-only ego calibration trend.

Read-only from the outside: nothing in Genesis's cognitive paths reads this
table. It exists so the USER can see whether the ego's stated confidence tracks
reality ("says 90%, right 82%") and whether that's improving over time. Injecting
calibration back into the ego is a deliberate, separately-flagged future PR.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)


async def record_snapshot(
    db: aiosqlite.Connection,
    *,
    domain: str,
    ece: float,
    mce: float,
    sample_count: int,
    bucket_count: int,
    low_confidence: bool,
    curve: list[dict],
) -> str:
    """Insert one calibration snapshot. ``curve`` is the per-bucket list,
    stored as JSON. Returns the snapshot id."""
    sid = uuid.uuid4().hex[:16]
    await db.execute(
        """INSERT INTO ego_calibration_snapshots
               (id, domain, ece, mce, sample_count, bucket_count,
                low_confidence, curve_json, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sid, domain, ece, mce, sample_count, bucket_count,
            1 if low_confidence else 0, json.dumps(curve),
            datetime.now(UTC).isoformat(),
        ),
    )
    await db.commit()
    return sid


async def get_latest(db: aiosqlite.Connection, *, domain: str = "ego") -> dict | None:
    """Most recent snapshot for a domain, or ``None`` if there are none yet
    (so the surface says "no data" instead of a spurious ECE=0.0)."""
    cur = await db.execute(
        "SELECT * FROM ego_calibration_snapshots "
        "WHERE domain = ? ORDER BY computed_at DESC LIMIT 1",
        (domain,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    snap = dict(zip(cols, row, strict=False))
    snap["curve"] = json.loads(snap.pop("curve_json")) if snap.get("curve_json") else []
    snap["low_confidence"] = bool(snap.get("low_confidence"))
    return snap


async def get_trend(
    db: aiosqlite.Connection, *, domain: str = "ego", limit: int = 30
) -> list[dict]:
    """ECE/MCE over time (newest first) — the self-improvement signal."""
    cur = await db.execute(
        "SELECT computed_at, ece, mce, sample_count, bucket_count, low_confidence "
        "FROM ego_calibration_snapshots WHERE domain = ? "
        "ORDER BY computed_at DESC LIMIT ?",
        (domain, limit),
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    out = [dict(zip(cols, r, strict=False)) for r in rows]
    for o in out:
        o["low_confidence"] = bool(o.get("low_confidence"))
    return out
