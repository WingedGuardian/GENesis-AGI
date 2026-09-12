"""Add ``cancelled_at`` to ``pending_outreach`` so a queued message can be CANCELLED.

The table could previously only express enqueued or DELIVERED. With no third
state, changing a queued reminder's delivery time left two bad options: send a
duplicate, or call ``mark_delivered`` on the original — which writes
``delivered = 1`` for a message that was never sent, into a store that
``outreach_queue`` reads back. A later session would then see the message as
delivered and conclude the recipient had been told something they had not.

ONE nullable timestamp rather than a ``cancelled`` flag plus a ``cancelled_at``,
deliberately diverging from the ``delivered`` / ``delivered_at`` pair above it:
NULL means live and non-NULL means cancelled-at-that-time, so there is no way to
represent the inconsistent "cancelled but no timestamp" state that a separate
boolean allows. Noted here because the divergence is a choice, not an oversight.

Additive, nullable, no backfill — existing rows are by definition not cancelled,
which is exactly what NULL means. Idempotent via ``PRAGMA table_info``.
Self-contained per migration convention — no genesis imports.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_outreach'"
    )
    if not await cursor.fetchone():
        return  # bare DB (runner unit tests) — nothing to alter

    col_cursor = await db.execute("PRAGMA table_info(pending_outreach)")
    cols = {row[1] for row in await col_cursor.fetchall()}

    if "cancelled_at" not in cols:
        await db.execute("ALTER TABLE pending_outreach ADD COLUMN cancelled_at TEXT")
