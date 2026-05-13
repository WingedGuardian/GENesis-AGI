"""CRUD operations for user_contacts table."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    name: str,
    relationship: str | None = None,
    organization: str | None = None,
    role: str | None = None,
    relevance: str | None = None,
    source: str = "conversation",
    linked_goal_ids: list[str] | None = None,
) -> str:
    """Create a new contact. Returns the contact ID."""
    contact_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO user_contacts
           (id, name, relationship, organization, role, relevance,
            last_mentioned, source, linked_goal_ids, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            contact_id, name, relationship, organization, role, relevance,
            now, source, json.dumps(linked_goal_ids or []),
            now, now,
        ),
    )
    await db.commit()
    return contact_id


async def update(
    db: aiosqlite.Connection,
    contact_id: str,
    **fields: object,
) -> bool:
    """Update contact fields. Returns True if row was updated."""
    allowed = {
        "name", "relationship", "organization", "role", "relevance",
        "last_mentioned", "interaction_count", "linked_goal_ids",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = datetime.now(UTC).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [contact_id]
    cursor = await db.execute(
        f"UPDATE user_contacts SET {set_clause} WHERE id = ?",  # noqa: S608
        values,
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_by_id(
    db: aiosqlite.Connection,
    contact_id: str,
) -> dict | None:
    """Get a single contact by ID."""
    cursor = await db.execute(
        "SELECT * FROM user_contacts WHERE id = ?", (contact_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def find_by_name(
    db: aiosqlite.Connection,
    name: str,
) -> dict | None:
    """Find a contact by exact name (case-insensitive)."""
    cursor = await db.execute(
        "SELECT * FROM user_contacts WHERE LOWER(name) = LOWER(?)",
        (name,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_all(
    db: aiosqlite.Connection,
    *,
    limit: int = 50,
) -> list[dict]:
    """List all contacts ordered by most recently mentioned."""
    cursor = await db.execute(
        "SELECT * FROM user_contacts "
        "ORDER BY last_mentioned DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def recently_active(
    db: aiosqlite.Connection,
    *,
    days: int = 14,
) -> list[dict]:
    """List contacts mentioned in the last N days."""
    cursor = await db.execute(
        "SELECT * FROM user_contacts "
        "WHERE last_mentioned >= datetime('now', ?) "
        "ORDER BY interaction_count DESC",
        (f"-{days} days",),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def reconnection_candidates(
    db: aiosqlite.Connection,
    *,
    stale_days: int = 30,
    limit: int = 10,
) -> list[dict]:
    """Contacts not mentioned in stale_days but linked to active goals."""
    cursor = await db.execute(
        "SELECT * FROM user_contacts "
        "WHERE last_mentioned < datetime('now', ?) "
        "AND linked_goal_ids != '[]' "
        "ORDER BY interaction_count DESC LIMIT ?",
        (f"-{stale_days} days", limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def record_mention(
    db: aiosqlite.Connection,
    contact_id: str,
    context: str | None = None,
) -> bool:
    """Record a new mention of a contact — update last_mentioned and count."""
    now = datetime.now(UTC).isoformat()

    # Append context note if provided
    if context:
        contact = await get_by_id(db, contact_id)
        if contact:
            try:
                notes = json.loads(contact.get("context_notes") or "[]")
            except (json.JSONDecodeError, TypeError):
                notes = []
            notes.append({"date": now[:10], "context": context})
            # Keep last 20 notes
            notes = notes[-20:]
            await db.execute(
                "UPDATE user_contacts SET context_notes = ? WHERE id = ?",
                (json.dumps(notes), contact_id),
            )

    cursor = await db.execute(
        "UPDATE user_contacts "
        "SET last_mentioned = ?, interaction_count = interaction_count + 1, "
        "    updated_at = ? "
        "WHERE id = ?",
        (now, now, contact_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(
    db: aiosqlite.Connection,
    contact_id: str,
) -> bool:
    """Delete a contact."""
    cursor = await db.execute(
        "DELETE FROM user_contacts WHERE id = ?", (contact_id,)
    )
    await db.commit()
    return cursor.rowcount > 0
