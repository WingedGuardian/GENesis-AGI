"""Record which client wrote a verification row (issue #4297).

Three nullable TEXT columns: ``closed_by`` (the client that closed the row),
``last_attempt_by`` (the client behind the latest attempt) and
``recorded_session`` (the transcript or thread id of the session that wrote
the latest close or attempt). The values come from
``genesis.caller_context.client_kind``, a label for records that grants nothing.

Existing rows keep NULL: who wrote them was never recorded, and nothing here
guesses. No CHECK and no destructive down migration. Column order matches the
canonical DDL; the runner owns commit and rollback. Fresh installs may already
have the columns.
"""

from __future__ import annotations

import aiosqlite

COLUMNS = ("closed_by", "last_attempt_by", "recorded_session")


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pr_verifications'"
    )
    if await cursor.fetchone() is None:
        return  # fresh install: create_all_tables builds the full canonical shape
    cursor = await db.execute("PRAGMA table_info(pr_verifications)")
    present = {row[1] for row in await cursor.fetchall()}
    for column in COLUMNS:
        if column not in present:
            await db.execute(f"ALTER TABLE pr_verifications ADD COLUMN {column} TEXT")


async def down(db: aiosqlite.Connection) -> None:
    """No-op, deliberately: the columns hold attribution nothing else records."""
