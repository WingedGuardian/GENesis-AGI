"""Add ``pending_issue_posts.adopted`` — create-vs-adopt provenance for WS-A.

The poster drain adopts a pre-existing OPEN issue whose title matches a proposal
(this covers BOTH crash-idempotency — Genesis created it then crashed before
``mark_posted`` — AND a coincidental external/human issue with the same title).
Only a Genesis-CREATED issue (or a crash-recovery adopt of an issue Genesis itself
authored) is an authoritative close-link; an adopted EXTERNAL issue must not
auto-resolve the originating follow_up. This column records that provenance so
``posted_index_for_repo`` (the close-loop join) can exclude adopted rows
(``adopted = 1``).

Idempotent (``_has_column`` guard). Fresh installs get the column from
``db/schema/_tables.py``; this migration covers existing installs. The column is
UNINDEXED, so it is deliberately NOT mirrored into ``_migrate_add_columns`` — per
the base-path parity guard (``test_schema_base_path_parity``), only INDEXED
migration-added columns need that mirror. A guarded mirror would be harmless
(``_try_alter`` suppresses duplicate-column) but is simply unnecessary here.
"""

from __future__ import annotations

import aiosqlite


async def _has_column(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in await cursor.fetchall())


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    # Let any ALTER error propagate: the _has_column guard gives idempotency (so a
    # duplicate-column can't fire), and a transient SQLITE_LOCKED reaches the
    # runner's retry-on-lock loop rather than being swallowed.
    if not await _has_column(db, "pending_issue_posts", "adopted"):
        await db.execute(
            "ALTER TABLE pending_issue_posts ADD COLUMN adopted INTEGER NOT NULL DEFAULT 0"
        )


async def down(db: aiosqlite.Connection) -> None:
    """SQLite cannot cheaply drop a column; leave it in place (additive, harmless)."""
