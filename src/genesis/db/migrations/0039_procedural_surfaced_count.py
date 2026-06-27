"""Add surfaced_count column to procedural_memory.

``surfaced_count`` tracks how many times a procedure was *surfaced* into a
model's context by a contextual hook (the proactive memory hook on a prompt, or
the PreToolUse procedure advisor on a tool call) — distinct from
``invocation_count`` (explicit recall via the ``procedure_recall`` MCP tool).

This is an HONEST funnel-observability counter ONLY. It is deliberately NOT read
by the promoter (``learning/procedural/promoter.py`` reads ``invocation_count``
alone), so passive surfacing can never promote an unproven DORMANT draft. Default
0; existing rows backfill via the column DEFAULT.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Table may not exist if migrations run on a fresh DB before schema init.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='procedural_memory'"
    )
    if not await cursor.fetchone():
        return

    # Only add column if it doesn't already exist (fresh DBs get it from the
    # canonical CREATE TABLE in db/schema/_tables.py).
    col_cursor = await db.execute("PRAGMA table_info(procedural_memory)")
    cols = {row[1] for row in await col_cursor.fetchall()}
    if "surfaced_count" not in cols:
        await db.execute(
            "ALTER TABLE procedural_memory "
            "ADD COLUMN surfaced_count INTEGER NOT NULL DEFAULT 0"
        )
