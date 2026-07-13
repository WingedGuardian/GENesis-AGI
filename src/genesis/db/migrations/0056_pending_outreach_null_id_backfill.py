"""Backfill NULL ``id`` rows in ``pending_outreach`` and clear the churn loop.

SQLite allows NULL in a ``TEXT PRIMARY KEY``, so legacy rows inserted before
``enqueue`` began stamping a uuid have ``id = NULL``. The drain job marks
delivery with ``WHERE id = ?`` → ``WHERE id = NULL`` matches zero rows, so such
a row never clears: every cycle it is re-drained, re-processed, and re-logged
("aged out … dropping") forever. The 24h age-out cap can't break the loop
because the cap's own drop *is* a ``mark_delivered`` call.

The CRUD fix (drain exposes ``rowid``; ``mark_delivered_by_rowid``) stops new
occurrences, but existing NULL-id rows must be reconciled once. Give each a
deterministic synthetic id and mark it delivered — these are, by definition,
legacy rows (the sanctioned insert path has stamped a uuid for months), so
dropping their (long-stale) delivery is correct.

Guarded ``WHERE id IS NULL`` → idempotent. No commit — the runner owns the
transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite


async def _has_table(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cursor.fetchone() is not None


async def up(db: aiosqlite.Connection) -> None:
    if not await _has_table(db, "pending_outreach"):
        return
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE pending_outreach "
        "SET id = 'nullid-backfill-' || rowid, "
        "    delivered = 1, "
        "    delivered_at = COALESCE(delivered_at, ?) "
        "WHERE id IS NULL",
        (now,),
    )
