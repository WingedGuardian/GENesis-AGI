"""Outreach stats snapshot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def outreach_stats(db: aiosqlite.Connection | None) -> dict:
    if not db:
        return {"status": "unknown"}

    try:
        cursor = await db.execute(
            """SELECT
                COUNT(*) as total,
                COUNT(delivered_at) as delivered,
                COUNT(opened_at) as opened,
                SUM(CASE WHEN engagement_outcome IN ('useful', 'engaged') THEN 1 ELSE 0 END) as engaged
            FROM outreach_history
            WHERE created_at >= datetime('now', '-7 days')
              AND category != 'digest'"""
        )
        row = await cursor.fetchone()
        total = row["total"] if row else 0
        delivered = row["delivered"] if row else 0
        opened = row["opened"] if row else 0
        engaged = row["engaged"] if row else 0

        return {
            "window": "7d",
            "total": total,
            "delivery_rate": round(delivered / total, 3) if total > 0 else None,
            "open_rate": round(opened / delivered, 3) if delivered > 0 else None,
            "engagement_rate": round(engaged / delivered, 3) if delivered > 0 else None,
        }
    except Exception:
        logger.warning("Failed to collect outreach stats", exc_info=True)
        return {"status": "unknown"}
