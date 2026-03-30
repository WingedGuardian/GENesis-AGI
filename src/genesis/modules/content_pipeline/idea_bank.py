"""IdeaBank — capture, rank, and manage content ideas."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.modules.content_pipeline.types import ContentIdea

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def ensure_table(db: aiosqlite.Connection) -> None:
    """Create the content_ideas table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS content_ideas (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            score REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'new',
            platform_target TEXT,
            created_at TEXT NOT NULL,
            planned_at TEXT,
            published_at TEXT
        )
    """)
    await db.commit()


def _row_to_idea(row: aiosqlite.Row) -> ContentIdea:
    """Convert a database row to a ContentIdea."""
    return ContentIdea(
        id=row[0],
        source=row[1],
        content=row[2],
        tags=json.loads(row[3]) if row[3] else [],
        score=row[4],
        status=row[5],
        platform_target=row[6],
        created_at=row[7],
        planned_at=row[8],
        published_at=row[9],
    )


class IdeaBank:
    """Manages content ideas with scoring and status tracking."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def capture(
        self,
        source: str,
        content: str,
        tags: list[str] | None = None,
        platform_target: str | None = None,
    ) -> ContentIdea:
        """Capture a new content idea."""
        idea = ContentIdea(
            id=str(uuid.uuid4()),
            source=source,
            content=content,
            tags=tags or [],
            score=0.0,
            status="new",
            platform_target=platform_target,
            created_at=datetime.now(UTC).isoformat(),
        )
        await self._db.execute(
            """INSERT INTO content_ideas
               (id, source, content, tags, score, status, platform_target, created_at, planned_at, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                idea.id,
                idea.source,
                idea.content,
                json.dumps(idea.tags),
                idea.score,
                idea.status,
                idea.platform_target,
                idea.created_at,
                idea.planned_at,
                idea.published_at,
            ),
        )
        await self._db.commit()
        logger.debug("Captured idea %s from %s", idea.id, source)
        return idea

    async def rank(self, limit: int = 20) -> list[ContentIdea]:
        """Return new ideas sorted by score descending."""
        cursor = await self._db.execute(
            "SELECT * FROM content_ideas WHERE status = 'new' ORDER BY score DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [_row_to_idea(row) for row in rows]

    async def list_by_status(self, status: str, limit: int = 50) -> list[ContentIdea]:
        """List ideas filtered by status."""
        cursor = await self._db.execute(
            "SELECT * FROM content_ideas WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_idea(row) for row in rows]

    async def update_status(self, idea_id: str, status: str) -> bool:
        """Update an idea's status. Returns True if the idea existed."""
        cursor = await self._db.execute(
            "UPDATE content_ideas SET status = ? WHERE id = ?",
            (status, idea_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def update_score(self, idea_id: str, score: float) -> bool:
        """Update an idea's score. Returns True if the idea existed."""
        cursor = await self._db.execute(
            "UPDATE content_ideas SET score = ? WHERE id = ?",
            (score, idea_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0
