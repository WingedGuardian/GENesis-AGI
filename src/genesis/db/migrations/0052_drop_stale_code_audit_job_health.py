"""Drop the stale job_health row for the removed schedule_code_audit job.

The scheduled job was removed (it had been disabled-by-config since the
surplus jobs extraction), but installs that ever ran it keep a persisted
job_health row that would otherwise surface as an ever-staler entry in every
job-health enumerator. Data-only; idempotent; nothing to recreate on down
(the job no longer exists).
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='job_health'"
    )
    if await cursor.fetchone():
        await db.execute(
            "DELETE FROM job_health WHERE job_name = 'schedule_code_audit'"
        )


async def down(db: aiosqlite.Connection) -> None:
    # Nothing to restore — the job this row described no longer exists.
    pass
