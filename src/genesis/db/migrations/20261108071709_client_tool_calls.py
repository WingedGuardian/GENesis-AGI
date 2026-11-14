"""Record which client made a tool call: ``client_tool_calls`` + ``credential_access_log.client``.

``client_tool_calls`` is an append-only log of the MCP tool calls a labelled
client makes (today: owner-mode Codex), one row per call: client, server, tool,
start time, how the call ended (``outcome``: returned / raised / cancelled, not
whether the tool succeeded), and the id of what it wrote for the few writer
tools known to return one. Never the
arguments, which can carry secrets. It answers "what did that client do, and
when" for an audit or an undo; it grants nothing.

New-Store Gate: ``activity_log`` keeps per-call metrics but is pruned within
about a day (measured 2026-11-08: its oldest row was under 30 hours old), and
it carries no client and no result id, so it cannot hold an audit trail. No
other store records tool calls. Retention: ``scripts/prune_client_tool_calls.py``
from ``scripts/disk_hygiene.sh``.

``credential_access_log.client`` stamps which client a credentials lookup came
from, set server-side, so a caller cannot forge it through ``accessor_context``.
Nullable, no backfill: an existing row reads NULL, meaning "no labelled client".

Additive and idempotent; DDL byte-identical to the ``db/schema/_tables.py``
mirror, and the column is mirrored in ``schema._migrations._migrate_add_columns``
(schema_both_build_paths). The runner owns the transaction — no commit here.
"""

from __future__ import annotations

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS client_tool_calls (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        client      TEXT NOT NULL,
        server      TEXT NOT NULL,
        tool        TEXT NOT NULL,
        called_at   TEXT NOT NULL,
        outcome     TEXT NOT NULL CHECK (outcome IN ('returned', 'raised', 'cancelled')),
        result_ref  TEXT
    )
    """
_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_client_tool_calls_client_time "
    "ON client_tool_calls(client, called_at)"
)


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(_TABLE_DDL)
    await db.execute(_INDEX_DDL)
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential_access_log'"
    )
    if await cursor.fetchone() is None:
        return  # Pre-base path: create_all_tables builds the canonical shape.
    cursor = await db.execute("PRAGMA table_info(credential_access_log)")
    if "client" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE credential_access_log ADD COLUMN client TEXT")


async def down(db: aiosqlite.Connection) -> None:
    """Destructive (dev/test affordance). The added column is left in place."""
    await db.execute("DROP TABLE IF EXISTS client_tool_calls")
