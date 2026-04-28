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
    """Create the content_publishes table if it doesn't exist, then migrate."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS content_publishes (
            id TEXT PRIMARY KEY,
            idea_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_text TEXT NOT NULL,
            published_at TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            platform_post_id TEXT,
            post_url TEXT,
            error_message TEXT,
            distributed_at TEXT
        )
    """)
    # Migrate existing tables that lack the new columns.
    cursor = await db.execute("PRAGMA table_info(content_publishes)")
    existing_cols = {row[1] for row in await cursor.fetchall()}
    for col, defn in [
        ("platform_post_id", "TEXT"),
        ("post_url", "TEXT"),
        ("error_message", "TEXT"),
        ("distributed_at", "TEXT"),
    ]:
        if col not in existing_cols:
            await db.execute(f"ALTER TABLE content_publishes ADD COLUMN {col} {defn}")
    await db.commit()


def _row_to_publish(
    row: aiosqlite.Row,
    col_names: list[str] | None = None,
) -> PublishResult:
    """Convert a database row to a PublishResult.

    Supports both positional (legacy) and named-column access.
    """
    if col_names is not None:
        d = dict(zip(col_names, row, strict=False))
        return PublishResult(
            id=d["id"],
            idea_id=d["idea_id"],
            platform=d["platform"],
            content_text=d["content_text"],
            published_at=d.get("published_at"),
            status=d.get("status", "draft"),
            platform_post_id=d.get("platform_post_id"),
            post_url=d.get("post_url"),
            error_message=d.get("error_message"),
            distributed_at=d.get("distributed_at"),
        )
    # Positional fallback for callers that don't pass col_names.
    return PublishResult(
        id=row[0],
        idea_id=row[1],
        platform=row[2],
        content_text=row[3],
        published_at=row[4],
        status=row[5],
        platform_post_id=row[6] if len(row) > 6 else None,
        post_url=row[7] if len(row) > 7 else None,
        error_message=row[8] if len(row) > 8 else None,
        distributed_at=row[9] if len(row) > 9 else None,
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
        col_names = [desc[0] for desc in cursor.description] if cursor.description else None
        return [_row_to_publish(row, col_names=col_names) for row in rows]

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
