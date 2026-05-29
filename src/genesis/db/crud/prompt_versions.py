"""CRUD operations for prompt_versions table — prompt versioning with outcome linkage."""

from __future__ import annotations

import hashlib
import logging

import aiosqlite

logger = logging.getLogger(__name__)


def compute_prompt_hash(prompt_text: str) -> str:
    """Compute a stable SHA-256 hash of a prompt's static content."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]


async def record_version(
    db: aiosqlite.Connection,
    *,
    prompt_hash: str,
    call_site: str,
    content_preview: str | None = None,
) -> None:
    """Record a prompt version if not already known.

    Uses INSERT OR IGNORE so repeated calls with the same hash+call_site
    are no-ops.  The first_seen timestamp is set only on first insert.
    """
    preview = content_preview[:200] if content_preview else None
    await db.execute(
        "INSERT OR IGNORE INTO prompt_versions (hash, call_site, content_preview) "
        "VALUES (?, ?, ?)",
        (prompt_hash, call_site, preview),
    )
    await db.commit()


async def get_versions(
    db: aiosqlite.Connection,
    call_site: str,
) -> list[dict]:
    """Return all known prompt versions for a call site, newest first."""
    cursor = await db.execute(
        "SELECT hash, call_site, first_seen, content_preview "
        "FROM prompt_versions WHERE call_site = ? ORDER BY first_seen DESC",
        (call_site,),
    )
    return [dict(row) for row in await cursor.fetchall()]
