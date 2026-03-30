"""ContentPlanner — plan and schedule content across platforms."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.modules.content_pipeline.types import ContentPlan, PlannedContent

if TYPE_CHECKING:
    import aiosqlite

    from genesis.content.drafter import ContentDrafter

logger = logging.getLogger(__name__)


async def ensure_table(db: aiosqlite.Connection) -> None:
    """Create the content_plans table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS content_plans (
            id TEXT PRIMARY KEY,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            items TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL
        )
    """)
    await db.commit()


def _serialize_items(items: list[PlannedContent]) -> str:
    """Serialize planned content items to JSON."""
    return json.dumps([
        {
            "idea_id": item.idea_id,
            "platform": item.platform,
            "scheduled_date": item.scheduled_date,
            "notes": item.notes,
        }
        for item in items
    ])


def _deserialize_items(raw: str) -> list[PlannedContent]:
    """Deserialize planned content items from JSON."""
    data = json.loads(raw) if raw else []
    return [
        PlannedContent(
            idea_id=d["idea_id"],
            platform=d["platform"],
            scheduled_date=d["scheduled_date"],
            notes=d.get("notes", ""),
        )
        for d in data
    ]


def _row_to_plan(row: aiosqlite.Row) -> ContentPlan:
    """Convert a database row to a ContentPlan."""
    return ContentPlan(
        id=row[0],
        period_start=row[1],
        period_end=row[2],
        items=_deserialize_items(row[3]),
        status=row[4],
        created_at=row[5],
    )


class ContentPlanner:
    """Creates and manages content plans."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        drafter: ContentDrafter | None = None,
    ) -> None:
        self._db = db
        self._drafter = drafter

    async def create_plan(
        self,
        ideas: list[dict],
        period_start: str,
        period_end: str,
        platforms: list[str] | None = None,
    ) -> ContentPlan:
        """Create a content plan from a list of ideas.

        Args:
            ideas: List of dicts with at least 'idea_id'. May include
                   'platform', 'scheduled_date', 'notes'.
            period_start: ISO date string for plan start.
            period_end: ISO date string for plan end.
            platforms: Default platforms if not specified per-idea.
        """
        default_platforms = platforms or ["generic"]
        items = []
        for idea in ideas:
            platform = idea.get("platform", default_platforms[0])
            items.append(PlannedContent(
                idea_id=idea["idea_id"],
                platform=platform,
                scheduled_date=idea.get("scheduled_date", period_start),
                notes=idea.get("notes", ""),
            ))

        plan = ContentPlan(
            id=str(uuid.uuid4()),
            period_start=period_start,
            period_end=period_end,
            items=items,
            status="draft",
            created_at=datetime.now(UTC).isoformat(),
        )
        await self._db.execute(
            """INSERT INTO content_plans (id, period_start, period_end, items, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (plan.id, plan.period_start, plan.period_end,
             _serialize_items(plan.items), plan.status, plan.created_at),
        )
        await self._db.commit()
        logger.debug("Created plan %s (%s to %s)", plan.id, period_start, period_end)
        return plan

    async def get_plan(self, plan_id: str) -> ContentPlan | None:
        """Get a plan by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM content_plans WHERE id = ?", (plan_id,),
        )
        row = await cursor.fetchone()
        return _row_to_plan(row) if row else None

    async def list_plans(
        self, status: str | None = None, limit: int = 10,
    ) -> list[ContentPlan]:
        """List plans, optionally filtered by status."""
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM content_plans WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM content_plans ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [_row_to_plan(row) for row in rows]

    async def update_plan_status(self, plan_id: str, status: str) -> bool:
        """Update a plan's status. Returns True if the plan existed."""
        cursor = await self._db.execute(
            "UPDATE content_plans SET status = ? WHERE id = ?",
            (status, plan_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0
