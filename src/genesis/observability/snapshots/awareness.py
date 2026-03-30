"""Awareness signals snapshot."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def awareness(db: aiosqlite.Connection | None) -> dict:
    if not db:
        return {"status": "unknown"}

    try:
        from genesis.db.crud import awareness_ticks

        last = await awareness_ticks.last_tick(db)
        if not last:
            return {"status": "no_ticks", "last_tick_at": None, "ticks_24h": 0}

        last_at = last.get("created_at", "")
        now = datetime.now(UTC)
        try:
            tick_time = datetime.fromisoformat(last_at)
            age_s = (now - tick_time).total_seconds()
        except (ValueError, TypeError):
            age_s = -1

        ticks_24h = await awareness_ticks.count_in_window_all(
            db, window_seconds=86400
        )
        critical_bypasses = await awareness_ticks.count_by_source(
            db, source="critical_bypass", window_seconds=86400
        )

        depth_dist = {}
        try:
            cursor = await db.execute(
                "SELECT classified_depth, COUNT(*) as cnt "
                "FROM awareness_ticks "
                "WHERE created_at >= datetime('now', '-1 day') "
                "GROUP BY classified_depth"
            )
            for row in await cursor.fetchall():
                depth = row[0] or "none"
                depth_dist[depth] = row[1]
        except Exception:
            pass

        last_reflection = None
        try:
            cursor = await db.execute(
                "SELECT created_at, source FROM observations "
                "WHERE source LIKE '%reflection%' "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                last_reflection = {"at": row[0], "source": row[1]}
        except Exception:
            pass

        return {
            "status": "healthy" if 0 < age_s < 360 else "overdue",
            "last_tick_at": last_at,
            "time_since_last_tick_seconds": round(age_s, 1) if age_s >= 0 else None,
            "ticks_24h": ticks_24h,
            "critical_bypasses_24h": critical_bypasses,
            "depth_distribution_24h": depth_dist,
            "last_reflection": last_reflection,
        }
    except Exception:
        logger.warning("Failed to collect awareness stats", exc_info=True)
        return {"status": "unknown"}
