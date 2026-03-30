"""CRUD operations for Behavioral Immune System tables.

GROUNDWORK(bis): Data layer for graduated behavioral correction.
Tables: behavioral_corrections, behavioral_themes, behavioral_treatments.
See docs/plans/2026-03-27-behavioral-immune-system-design.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


# ── Corrections ──────────────────────────────────────────────────────────────


async def create_correction(
    db: aiosqlite.Connection,
    *,
    raw_user_text: str,
    context: str,
    severity: float = 0.5,
    theme_id: str | None = None,
    embedding_id: str | None = None,
) -> str:
    """Record a raw behavioral correction from the user. Returns correction ID."""
    cid = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO behavioral_corrections
           (id, raw_user_text, context, severity, theme_id, embedding_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (cid, raw_user_text, context, severity, theme_id, embedding_id, now),
    )
    await db.commit()
    return cid


async def get_corrections_by_theme(
    db: aiosqlite.Connection,
    theme_id: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Fetch corrections linked to a specific theme."""
    cursor = await db.execute(
        """SELECT * FROM behavioral_corrections
           WHERE theme_id = ? ORDER BY created_at DESC LIMIT ?""",
        (theme_id, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_unthemed_corrections(
    db: aiosqlite.Connection,
    *,
    limit: int = 100,
) -> list[dict]:
    """Fetch corrections not yet assigned to a theme (for clustering)."""
    cursor = await db.execute(
        """SELECT * FROM behavioral_corrections
           WHERE theme_id IS NULL ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


# ── Themes ───────────────────────────────────────────────────────────────────


async def create_theme(
    db: aiosqlite.Connection,
    *,
    name: str,
    description: str,
) -> str:
    """Create a new behavioral theme. Returns theme ID."""
    tid = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO behavioral_themes
           (id, name, description, correction_count, last_correction_at, created_at)
           VALUES (?, ?, ?, 0, NULL, ?)""",
        (tid, name, description, now),
    )
    await db.commit()
    return tid


async def increment_theme_count(
    db: aiosqlite.Connection,
    theme_id: str,
) -> None:
    """Bump correction_count and last_correction_at for a theme."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """UPDATE behavioral_themes
           SET correction_count = correction_count + 1, last_correction_at = ?
           WHERE id = ?""",
        (now, theme_id),
    )
    await db.commit()


# ── Treatments ───────────────────────────────────────────────────────────────


async def create_treatment(
    db: aiosqlite.Connection,
    *,
    theme_id: str,
    treatment_type: str,
    treatment_ref: str,
    level: int = 0,
    branch: str = "hook",
) -> str:
    """Create a treatment for a behavioral theme. Returns treatment ID."""
    import json

    tid = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    history = json.dumps([{"action": "created", "level": level, "at": now}])
    await db.execute(
        """INSERT INTO behavioral_treatments
           (id, theme_id, treatment_type, treatment_ref, level, branch,
            status, violation_count, adjustment_history, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?)""",
        (tid, theme_id, treatment_type, treatment_ref, level, branch, history, now),
    )
    await db.commit()
    return tid


async def get_treatments_for_theme(
    db: aiosqlite.Connection,
    theme_id: str,
) -> list[dict]:
    """Get all treatments for a theme."""
    cursor = await db.execute(
        """SELECT * FROM behavioral_treatments
           WHERE theme_id = ? ORDER BY level""",
        (theme_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]
