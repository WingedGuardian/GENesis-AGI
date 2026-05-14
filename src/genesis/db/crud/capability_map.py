"""CRUD operations for the capability map.

Stores the ego's self-model: per-domain confidence scores derived
from aggregating intervention journal, proposals, autonomy state,
procedural memory, and CC session outcomes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)


_TREND_THRESHOLD = 0.05  # 5% change threshold for trend detection


async def upsert(
    db: aiosqlite.Connection,
    *,
    domain: str,
    confidence: float,
    sample_size: int,
    trend: str = "stable",
    evidence_summary: str = "",
) -> str:
    """Insert or update a capability map entry for a domain.

    Automatically computes trend by comparing new confidence against
    the existing score (>5% change = improving/declining).
    """
    now = datetime.now(UTC).isoformat()
    cid = uuid.uuid4().hex[:16]

    # Read current confidence for trend detection
    cur = await db.execute(
        "SELECT confidence FROM capability_map WHERE domain = ?",
        (domain,),
    )
    row = await cur.fetchone()
    previous_confidence = row[0] if row else None

    # Compute trend from delta (only when we have previous data + enough samples)
    if previous_confidence is not None and sample_size >= 3:
        delta = confidence - previous_confidence
        if delta > _TREND_THRESHOLD:
            trend = "improving"
        elif delta < -_TREND_THRESHOLD:
            trend = "declining"
        else:
            trend = "stable"

    await db.execute(
        """INSERT INTO capability_map
           (id, domain, confidence, sample_size, trend, evidence_summary,
            updated_at, previous_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(domain) DO UPDATE SET
             previous_confidence = capability_map.confidence,
             confidence = excluded.confidence,
             sample_size = excluded.sample_size,
             trend = excluded.trend,
             evidence_summary = excluded.evidence_summary,
             updated_at = excluded.updated_at""",
        (cid, domain, confidence, sample_size, trend, evidence_summary,
         now, previous_confidence),
    )
    await db.commit()
    return cid


async def get_all(db: aiosqlite.Connection) -> list[dict]:
    """Return all capability map entries ordered by confidence descending."""
    cur = await db.execute(
        "SELECT domain, confidence, sample_size, trend, evidence_summary, updated_at "
        "FROM capability_map ORDER BY confidence DESC"
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def get_by_domain(db: aiosqlite.Connection, domain: str) -> dict | None:
    """Fetch a single domain's capability entry."""
    cur = await db.execute(
        "SELECT domain, confidence, sample_size, trend, evidence_summary, updated_at "
        "FROM capability_map WHERE domain = ?",
        (domain,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=False))
