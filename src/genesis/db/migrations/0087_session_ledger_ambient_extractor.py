"""Widen session_ledger.added_by to admit the ambient ledger extractor.

The extractor (Haiku, run detached at each PreCompact boundary) has produced
shadow proposals for weeks but had no way to write a live ledger row: the write
path did not exist, and `added_by` is constrained at the SCHEMA level, not just
in Python.

WHY A NEW VALUE RATHER THAN REUSING 'ambient'. `_default_added_by()` already
returns 'ambient' for any DISPATCHED Claude Code session
(``GENESIS_CC_SESSION=1``), and the shadow report's leak invariant keys on
exactly that value to assert the extractor has written nothing live. Reusing it
would make that invariant unable to tell the extractor from a dispatched
session -- it would stop meaning anything on the very day it starts mattering.
A distinct value keeps the invariant a typed check, and makes extractor rows
filterable and bulk-revertible.

SQLite cannot ALTER a CHECK constraint, so this is the documented 12-step table
rebuild (precedent: 0012_ego_proposals_status_check).

IDEMPOTENCY IS KEYED ON THE CHECK DDL ITSELF, deliberately. Migration 0007
guarded its own rebuild on a COLUMN's existence, an earlier ALTER added that
column first, and 0007 then short-circuited and silently never applied its
constraint change -- which is why 0012 had to exist at all. Guarding on the
thing this migration actually changes is the only check that cannot lie.
"""

from __future__ import annotations

import aiosqlite

_NEW_VALUE = "ambient_ledger_extractor"


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_ledger'"
    )
    row = await cursor.fetchone()
    if not row:
        # Table absent: a fresh install creates it from _tables.py, which
        # already carries the widened constraint.
        return

    ddl = row[0] or ""
    if _NEW_VALUE in ddl:
        return  # already widened

    # A prior attempt may have died between CREATE and RENAME.
    await db.execute("DROP TABLE IF EXISTS session_ledger_new")

    await db.execute(
        """
        CREATE TABLE session_ledger_new (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            text        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','in_progress','done','absorbed','dropped')),
            source_ref  TEXT,
            added_by    TEXT NOT NULL DEFAULT 'foreground'
                        CHECK(added_by IN ('foreground','ambient','pulse',
                                           'ambient_ledger_extractor')),
            evidence    TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT
        )
        """
    )

    await db.execute(
        """
        INSERT INTO session_ledger_new
            (id, session_id, text, status, source_ref, added_by,
             evidence, created_at, updated_at)
        SELECT
            id, session_id, text, status, source_ref, added_by,
            evidence, created_at, updated_at
        FROM session_ledger
        """
    )

    await db.execute("DROP TABLE session_ledger")
    await db.execute("ALTER TABLE session_ledger_new RENAME TO session_ledger")

    # The rebuild drops the table's indexes with it.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_ledger_session "
        "ON session_ledger(session_id, status)"
    )
