"""Add ``thread_id`` + ``validated_recipient`` to ``pending_outreach``.

Subprocess (``pipeline=None``) ``outreach_send`` calls fall back to enqueueing
into ``pending_outreach``; the fallback dropped the thread id and resolved
recipient, so a queued email follow-up arrived recipient-less and the
genesis-server drain defaulted it to the agent's own address — a self-send spam
loop. Carrying both columns lets the drain reconstruct a properly-routed
``OutreachRequest`` (recipient + reply-vs-cold classification).

Additive, nullable, no backfill (old rows are telegram queue entries or the
now-cleared self-send rows). Idempotent via ``PRAGMA table_info``. Self-contained
per migration convention — no genesis imports.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='pending_outreach'"
    )
    if not await cursor.fetchone():
        return  # bare DB (runner unit tests) — nothing to alter

    col_cursor = await db.execute("PRAGMA table_info(pending_outreach)")
    cols = {row[1] for row in await col_cursor.fetchall()}

    if "thread_id" not in cols:
        await db.execute("ALTER TABLE pending_outreach ADD COLUMN thread_id TEXT")
    if "validated_recipient" not in cols:
        await db.execute(
            "ALTER TABLE pending_outreach ADD COLUMN validated_recipient TEXT"
        )
