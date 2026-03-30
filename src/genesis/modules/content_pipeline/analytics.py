"""AnalyticsTracker — record and analyze content performance."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.modules.content_pipeline.types import AnalyticsInsight, ContentMetrics

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def ensure_table(db: aiosqlite.Connection) -> None:
    """Create the content_metrics table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS content_metrics (
            id TEXT PRIMARY KEY,
            content_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            views INTEGER NOT NULL DEFAULT 0,
            likes INTEGER NOT NULL DEFAULT 0,
            shares INTEGER NOT NULL DEFAULT 0,
            fetched_at TEXT NOT NULL
        )
    """)
    await db.commit()


def _row_to_metrics(row: aiosqlite.Row) -> ContentMetrics:
    """Convert a database row to ContentMetrics."""
    return ContentMetrics(
        content_id=row[1],
        platform=row[2],
        views=row[3],
        likes=row[4],
        shares=row[5],
        fetched_at=row[6],
    )


class AnalyticsTracker:
    """Records and analyzes content performance metrics."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def record_metrics(
        self,
        content_id: str,
        platform: str,
        views: int = 0,
        likes: int = 0,
        shares: int = 0,
    ) -> None:
        """Record a metrics snapshot for a content piece."""
        metric_id = str(uuid.uuid4())
        fetched_at = datetime.now(UTC).isoformat()
        await self._db.execute(
            """INSERT INTO content_metrics
               (id, content_id, platform, views, likes, shares, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (metric_id, content_id, platform, views, likes, shares, fetched_at),
        )
        await self._db.commit()
        logger.debug("Recorded metrics for %s on %s", content_id, platform)

    async def get_metrics(self, content_id: str) -> list[ContentMetrics]:
        """Get all metric snapshots for a content piece."""
        cursor = await self._db.execute(
            "SELECT * FROM content_metrics WHERE content_id = ? ORDER BY fetched_at DESC",
            (content_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_metrics(row) for row in rows]

    async def generate_insights(self, period_days: int = 7) -> AnalyticsInsight:
        """Generate analytics insights for the recent period.

        Ranks content by total engagement (views + likes*5 + shares*10)
        and identifies top/under performers.
        """
        # Get aggregated metrics per content_id
        cursor = await self._db.execute(
            """SELECT content_id,
                      SUM(views) as total_views,
                      SUM(likes) as total_likes,
                      SUM(shares) as total_shares,
                      (SUM(views) + SUM(likes) * 5 + SUM(shares) * 10) as engagement
               FROM content_metrics
               WHERE fetched_at >= datetime('now', ?)
               GROUP BY content_id
               ORDER BY engagement DESC""",
            (f"-{period_days} days",),
        )
        rows = await cursor.fetchall()

        if not rows:
            return AnalyticsInsight(
                period=f"last_{period_days}_days",
                top_performing=[],
                underperforming=[],
                recommendations=["No metrics data available for this period."],
            )

        top_performing = [row[0] for row in rows[:3]]
        underperforming = [row[0] for row in rows[-3:]] if len(rows) > 3 else []

        recommendations = []
        if rows:
            best = rows[0]
            recommendations.append(
                f"Top content ({best[0]}) got {best[1]} views, {best[2]} likes, {best[3]} shares.",
            )
        if len(rows) >= 2:
            recommendations.append(
                "Consider analyzing what differentiates top from bottom performers.",
            )

        return AnalyticsInsight(
            period=f"last_{period_days}_days",
            top_performing=top_performing,
            underperforming=underperforming,
            recommendations=recommendations,
        )
