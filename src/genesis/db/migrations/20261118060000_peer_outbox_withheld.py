"""Record that a peer collected an outbox message but could not read it (issue #4857).

One nullable TEXT column, ``withheld_category``. NULL means the ACK reported
delivery (or no ACK yet); a value from ``peers.outbox.WITHHELD_CATEGORIES``
means the peer withheld it. Existing rows keep NULL: nothing recorded a withheld
outcome before this, and nothing here guesses. Column order matches the
canonical DDL; the runner owns commit and rollback. Fresh installs may already
have the column.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='peer_outbox'"
    )
    if await cursor.fetchone() is None:
        return  # fresh install: create_all_tables builds the full canonical shape
    cursor = await db.execute("PRAGMA table_info(peer_outbox)")
    if "withheld_category" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE peer_outbox ADD COLUMN withheld_category TEXT")


async def down(db: aiosqlite.Connection) -> None:
    """No-op, deliberately: the column holds peer-reported outcomes nothing else records."""
