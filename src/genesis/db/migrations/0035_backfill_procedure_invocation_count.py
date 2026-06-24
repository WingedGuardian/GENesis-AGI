"""Backfill procedural_memory.invocation_count from procedure_invoked history.

The reads signal (a deliberate procedure_recall) is now tracked on the
``invocation_count`` column, but the column was dead until it was wired — the
historical reads live only as ``procedure_invoked`` events in ``eval_events``.
This one-time migration seeds the column from that history so the read signal
(used for recall ranking + tier promotion) doesn't start at zero.

Authoritative SET (not increment): at migration time the column is 0 and the
events are the historical record, so ``count`` is correct. Going forward the
column and events grow in lockstep (both written in ``procedure_recall``). The
versioned runner applies this exactly once.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Fresh installs may not have eval_events yet — nothing to backfill.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='eval_events'"
    )
    if not await cursor.fetchone():
        return

    await db.execute(
        """
        UPDATE procedural_memory
        SET invocation_count = (
            SELECT COUNT(*) FROM eval_events e
            WHERE e.event_type = 'procedure_invoked'
              AND e.subject_id = procedural_memory.id
        )
        WHERE id IN (
            SELECT DISTINCT subject_id FROM eval_events
            WHERE event_type = 'procedure_invoked' AND subject_id IS NOT NULL
        )
        """
    )
