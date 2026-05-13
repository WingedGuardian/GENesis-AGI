"""CRUD operations for user_goals table."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    title: str,
    category: str,
    description: str | None = None,
    priority: str = "medium",
    status: str = "active",
    timeline: str | None = None,
    parent_goal_id: str | None = None,
    evidence_source: str | None = None,
    confidence: float = 0.5,
) -> str:
    """Create a new goal. Returns the goal ID."""
    goal_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO user_goals
           (id, title, category, description, priority, status,
            timeline, parent_goal_id, evidence_source, confidence,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            goal_id, title, category, description, priority, status,
            timeline, parent_goal_id, evidence_source, confidence,
            now, now,
        ),
    )
    await db.commit()
    return goal_id


async def update(
    db: aiosqlite.Connection,
    goal_id: str,
    **fields: object,
) -> bool:
    """Update goal fields. Returns True if row was updated."""
    allowed = {
        "title", "description", "category", "priority", "status",
        "timeline", "parent_goal_id", "evidence_source", "confidence",
        "achieved_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = datetime.now(UTC).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [goal_id]
    cursor = await db.execute(
        f"UPDATE user_goals SET {set_clause} WHERE id = ?",  # noqa: S608
        values,
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_active(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[dict]:
    """List active goals ordered by priority (critical first)."""
    cursor = await db.execute(
        """SELECT * FROM user_goals
           WHERE status = 'active'
           ORDER BY
             CASE priority
               WHEN 'critical' THEN 0
               WHEN 'high' THEN 1
               WHEN 'medium' THEN 2
               WHEN 'low' THEN 3
             END,
             created_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_category(
    db: aiosqlite.Connection,
    category: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """List goals by category."""
    cursor = await db.execute(
        "SELECT * FROM user_goals WHERE category = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (category, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_by_id(
    db: aiosqlite.Connection,
    goal_id: str,
) -> dict | None:
    """Get a single goal by ID."""
    cursor = await db.execute(
        "SELECT * FROM user_goals WHERE id = ?", (goal_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_achieved(
    db: aiosqlite.Connection,
    goal_id: str,
) -> bool:
    """Mark a goal as achieved."""
    now = datetime.now(UTC).isoformat()
    return await update(db, goal_id, status="achieved", achieved_at=now)


async def mark_abandoned(
    db: aiosqlite.Connection,
    goal_id: str,
) -> bool:
    """Mark a goal as abandoned."""
    return await update(db, goal_id, status="abandoned")


async def add_progress_note(
    db: aiosqlite.Connection,
    goal_id: str,
    note: str,
) -> bool:
    """Append a progress note to a goal."""
    goal = await get_by_id(db, goal_id)
    if not goal:
        return False
    try:
        notes = json.loads(goal.get("progress_notes") or "[]")
    except (json.JSONDecodeError, TypeError):
        notes = []
    notes.append({
        "date": datetime.now(UTC).isoformat()[:10],
        "note": note,
    })
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE user_goals SET progress_notes = ?, updated_at = ? WHERE id = ?",
        (json.dumps(notes), now, goal_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def find_similar(
    db: aiosqlite.Connection,
    title: str,
    *,
    threshold: float = 0.6,
) -> dict | None:
    """Find an existing goal with a similar title (simple word overlap).

    Returns the best match above the threshold, or None.
    """
    goals = await list_active(db, limit=50)
    if not goals:
        return None

    title_words = set(title.lower().split())
    best_match = None
    best_score = 0.0

    for goal in goals:
        goal_words = set(goal["title"].lower().split())
        if not title_words or not goal_words:
            continue
        intersection = title_words & goal_words
        union = title_words | goal_words
        score = len(intersection) / len(union) if union else 0.0
        if score > best_score and score >= threshold:
            best_score = score
            best_match = goal

    return best_match
