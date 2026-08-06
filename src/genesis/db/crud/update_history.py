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


async def last_successful_update(db: aiosqlite.Connection) -> tuple[str, str] | None:
    """``(completed_at, new_commit)`` of the most recent successful update.

    None when the table is empty (pre-first-update install). Ordered by
    ``datetime(completed_at)`` — the deploy-staleness age axis cares about
    when the update FINISHED, and datetime() compares mixed-offset ISO
    timestamps by true instant."""
    cur = await db.execute(
        "SELECT completed_at, new_commit FROM update_history "
        "WHERE status = 'success' ORDER BY datetime(completed_at) DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return None
    return str(row[0]), str(row[1] or "")
