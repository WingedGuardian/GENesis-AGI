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
    goal_type: str = "milestone",
    cadence_days: int | None = None,
) -> str:
    """Create a new goal. Returns the goal ID."""
    goal_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO user_goals
           (id, title, category, description, priority, status,
            timeline, parent_goal_id, evidence_source, confidence,
            goal_type, cadence_days, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            goal_id, title, category, description, priority, status,
            timeline, parent_goal_id, evidence_source, confidence,
            goal_type, cadence_days, now, now,
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
        "achieved_at", "goal_type", "cadence_days",
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


async def list_children(
    db: aiosqlite.Connection,
    parent_goal_id: str,
    *,
    include_achieved: bool = False,
) -> list[dict]:
    """List child goals of a parent, ordered by priority."""
    if include_achieved:
        cursor = await db.execute(
            "SELECT * FROM user_goals WHERE parent_goal_id = ? "
            "ORDER BY CASE priority "
            "  WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "  WHEN 'medium' THEN 2 ELSE 3 END, created_at",
            (parent_goal_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM user_goals WHERE parent_goal_id = ? "
            "AND status = 'active' "
            "ORDER BY CASE priority "
            "  WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "  WHEN 'medium' THEN 2 ELSE 3 END, created_at",
            (parent_goal_id,),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def check_completion_cascade(
    db: aiosqlite.Connection,
    goal_id: str,
) -> dict | None:
    """Check if achieving this goal completes all siblings of a parent.

    Called after a child goal is marked achieved. If the parent is a
    milestone goal and ALL its children are now achieved, returns the
    parent info so the caller can surface a recommendation.

    Returns ``{"parent_id": ..., "parent_title": ...}`` if cascade is
    ready, or None otherwise.
    """
    goal = await get_by_id(db, goal_id)
    if not goal or not goal.get("parent_goal_id"):
        return None

    parent_id = goal["parent_goal_id"]
    parent = await get_by_id(db, parent_id)
    if not parent:
        return None

    # Continuous goals don't cascade — they're ongoing by definition
    if parent.get("goal_type") == "continuous":
        return None

    # Already achieved — no need to cascade again
    if parent.get("status") == "achieved":
        return None

    # Check all children of this parent
    children = await list_children(db, parent_id, include_achieved=True)
    if not children:
        return None

    all_achieved = all(c.get("status") == "achieved" for c in children)
    if not all_achieved:
        return None

    return {"parent_id": parent_id, "parent_title": parent.get("title", "?")}
