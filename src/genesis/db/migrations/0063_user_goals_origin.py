"""Add ``user_goals.origin`` — goal provenance (additive ego autonomy).

Who owns a goal: ``'user'`` (a user directive — the default) or
``'genesis_ego'`` (created by the Genesis ego for its own agenda). This is
the security boundary for additive ego autonomy: the ego may autonomously
pause/deprioritize ONLY goals it created; any mutation of a user-origin goal
stays gated behind an approved ``goal_status_change`` proposal. The column is
deliberately immutable after create — ``user_goals.update()`` excludes it
from its allow-list, so no path can relabel a user goal into ego control.

``NOT NULL DEFAULT 'user'`` — every pre-existing goal predates ego autonomy,
so it IS a user directive (never autonomously touchable). The CHECK makes an
unknown origin a hard integrity error, not a silent third state.

Fresh/test DBs get the column from the canonical CREATE TABLE in
``db/schema/_tables.py``; this numbered migration covers the existing-DB
upgrade path. Idempotent: PRAGMA-guarded ADD COLUMN. No commit — the runner
owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_goals'"
    )
    if await cursor.fetchone():
        col_cursor = await db.execute("PRAGMA table_info(user_goals)")
        cols = {row[1] for row in await col_cursor.fetchall()}
        if "origin" not in cols:
            await db.execute(
                "ALTER TABLE user_goals "
                "ADD COLUMN origin TEXT NOT NULL DEFAULT 'user' "
                "CHECK (origin IN ('user', 'genesis_ego'))"
            )
