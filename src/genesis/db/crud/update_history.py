"""CRUD reader for the ``update_history`` ledger (Genesis self-update attempts).

Writes are owned by ``scripts/update.sh`` (the deploy script records each run);
this module provides read access through the CRUD layer so callers never issue
raw SQL against ``genesis.db``.
"""

from __future__ import annotations

import aiosqlite


async def last_successful_deploy_commit(db: aiosqlite.Connection) -> str | None:
    """Return ``new_commit`` of the most recent successful update, else None.

    Ordered by ``datetime(started_at)`` so ISO timestamps with differing
    timezone offsets compare by true instant rather than lexicographically.
    """
    cur = await db.execute(
        "SELECT new_commit FROM update_history "
        "WHERE status = 'success' AND new_commit IS NOT NULL "
        "ORDER BY datetime(started_at) DESC LIMIT 1",
    )
    row = await cur.fetchone()
    return str(row[0]).strip() if row and row[0] else None
