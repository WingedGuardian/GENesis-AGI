"""Add ``run_requested_at`` to ``user_jobs`` — the cross-process run-now channel.

The standalone MCP server process has no scheduler: it shares only the DB with
the Genesis server. ``user_job_control(action="run_now")`` therefore writes this
timestamp instead of calling into a scheduler that does not exist in its
process; the server's ``UserJobScheduler`` picks it up on its next reconcile
tick and dispatches, then clears it. NULL means no pending request.

Additive, nullable, no backfill. Idempotent via ``PRAGMA table_info``.
Self-contained per migration convention — no genesis imports.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_jobs'"
    )
    if not await cursor.fetchone():
        return  # bare DB (runner unit tests) — nothing to alter

    col_cursor = await db.execute("PRAGMA table_info(user_jobs)")
    cols = {row[1] for row in await col_cursor.fetchall()}

    if "run_requested_at" not in cols:
        await db.execute(
            "ALTER TABLE user_jobs ADD COLUMN run_requested_at TEXT"
        )
