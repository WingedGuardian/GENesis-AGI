"""Engagement tracking — timeout detection and reply recording."""

from __future__ import annotations

import logging

import aiosqlite

from genesis.db.crud import outreach as outreach_crud

logger = logging.getLogger(__name__)


class EngagementTracker:
    """Tracks user engagement with outreach messages."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def check_timeouts(self, timeout_hours: int = 24) -> int:
        cursor = await self._db.execute(
            "SELECT id FROM outreach_history "
            "WHERE delivered_at IS NOT NULL "
            "AND engagement_outcome IS NULL "
            "AND delivered_at < datetime('now', ? || ' hours')",
            (f"-{timeout_hours}",),
        )
        rows = await cursor.fetchall()
        count = 0
        for row in rows:
            outreach_id = row[0] if isinstance(row, tuple) else row["id"]
            await outreach_crud.record_engagement(
                self._db, outreach_id, engagement_outcome="ignored", engagement_signal="timeout",
            )
            count += 1
        if count:
            logger.info("Marked %d outreach items as ignored (timeout)", count)
        return count

    async def find_outreach_for_reply(self, delivery_id: str) -> str | None:
        row = await outreach_crud.find_by_delivery_id(self._db, delivery_id)
        if row and row.get("engagement_outcome") is None:
            return row["id"]
        return None

    async def record_reply(self, outreach_id: str, reply_text: str) -> bool:
        """Record that the user replied to an outreach message."""
        try:
            await self._db.execute(
                "UPDATE outreach_history SET user_response = ?, "
                "engagement_outcome = 'useful', engagement_signal = 'user_reply' "
                "WHERE id = ?",
                (reply_text[:2000], outreach_id),
            )
            await self._db.commit()
            logger.info("Recorded reply engagement for outreach %s", outreach_id)
            return True
        except Exception:
            logger.warning("Failed to record reply engagement for %s", outreach_id, exc_info=True)
            return False

    async def record_implicit_engagement(self, outreach_id: str) -> bool:
        """Record that the user was active after receiving outreach (weak signal).

        Only upgrades NULL → ambivalent. Never downgrades engaged → ambivalent.
        """
        try:
            cursor = await self._db.execute(
                "UPDATE outreach_history SET engagement_outcome = 'ambivalent', "
                "engagement_signal = 'implicit_activity' "
                "WHERE id = ? AND engagement_outcome IS NULL",
                (outreach_id,),
            )
            await self._db.commit()
            if cursor.rowcount > 0:
                logger.debug("Recorded implicit engagement for outreach %s", outreach_id)
                return True
            return False
        except Exception:
            logger.debug("Failed to record implicit engagement for %s", outreach_id, exc_info=True)
            return False
