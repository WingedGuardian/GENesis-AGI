"""PublishManager — manage content publishing workflow."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.modules.content_pipeline.types import PublishResult, Script

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def ensure_table(db: aiosqlite.Connection) -> None:
    """Create the content_publishes table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS content_publishes (
            id TEXT PRIMARY KEY,
            idea_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_text TEXT NOT NULL,
            published_at TEXT,
            status TEXT NOT NULL DEFAULT 'draft'
        )
    """)
    await db.commit()


def _row_to_publish(row: aiosqlite.Row) -> PublishResult:
    """Convert a database row to a PublishResult."""
    return PublishResult(
        id=row[0],
        idea_id=row[1],
        platform=row[2],
        content_text=row[3],
        published_at=row[4],
        status=row[5],
    )


class PublishManager:
    """Manages content publishing across platforms."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def publish(
        self, script: Script, platforms: list[str],
    ) -> list[PublishResult]:
        """Create draft publish entries for given platforms.

        Actual delivery is handled by outreach/platform connectors.
        This records the intent and tracks status.
        """
        results = []
        for platform in platforms:
            result = PublishResult(
                id=str(uuid.uuid4()),
                idea_id=script.idea_id,
                platform=platform,
                content_text=script.content,
                published_at=None,
                status="draft",
            )
            await self._db.execute(
                """INSERT INTO content_publishes
                   (id, idea_id, platform, content_text, published_at, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    result.id,
                    result.idea_id,
                    result.platform,
                    result.content_text,
                    result.published_at,
                    result.status,
                ),
            )
            results.append(result)

        await self._db.commit()
        logger.debug(
            "Created %d publish entries for idea %s",
            len(results),
            script.idea_id,
        )
        return results

    async def get_publishes(
        self,
        idea_id: str | None = None,
        status: str | None = None,
    ) -> list[PublishResult]:
        """Get publish results, optionally filtered."""
        conditions = []
        params: list[str] = []
        if idea_id:
            conditions.append("idea_id = ?")
            params.append(idea_id)
        if status:
            conditions.append("status = ?")
            params.append(status)

        query = "SELECT * FROM content_publishes"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY rowid DESC"

        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_publish(row) for row in rows]

    async def update_publish_status(
        self, publish_id: str, status: str,
    ) -> bool:
        """Update a publish entry's status. Returns True if it existed."""
        now = None
        if status == "published":
            now = datetime.now(UTC).isoformat()

        if now:
            cursor = await self._db.execute(
                "UPDATE content_publishes SET status = ?, published_at = ? WHERE id = ?",
                (status, now, publish_id),
            )
        else:
            cursor = await self._db.execute(
                "UPDATE content_publishes SET status = ? WHERE id = ?",
                (status, publish_id),
            )
        await self._db.commit()
        return cursor.rowcount > 0
