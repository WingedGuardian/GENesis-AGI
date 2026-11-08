"""CRUD for client_tool_calls — which labelled client made which MCP tool call.

Append-only. One row per tool call made by a labelled client (owner-mode Codex
today): client, server, tool, start time, how the call ended (``outcome``), and
the id of what it wrote for the few writer tools known to return one. Arguments are never stored; they can carry secrets.

Written from stdio MCP subprocesses, which do not run migrations, so ``record``
guards on table existence and returns False before the migration has landed. It
never creates the table: migration 20261108071709 is the schema authority.
Retention: ``scripts/prune_client_tool_calls.py`` (disk hygiene timer).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

# Why 180 days: the log exists to answer "what did that client do" when
# something it wrote is questioned later, which can surface months after the
# call. A row is about 100 bytes, so even a heavy 1,000 calls a day for 180 days
# is roughly 18 MB. The prune script takes --days for an operator who wants
# another window.
DEFAULT_RETENTION_DAYS = 180

# Once the table is confirmed present, stop re-checking. A missing table is
# re-checked on every call so a writer self-heals when the migration lands.
_table_verified = False


async def _table_available(db: aiosqlite.Connection) -> bool:
    global _table_verified
    if _table_verified:
        return True
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_tool_calls'"
    )
    exists = await cursor.fetchone() is not None
    _table_verified = exists
    return exists


async def record(
    db: aiosqlite.Connection,
    *,
    client: str,
    server: str,
    tool: str,
    called_at: str,
    outcome: str,
    result_ref: str | None,
) -> bool:
    """Append one call. False (no row) when the table does not exist yet.

    ``outcome`` is ``returned``, ``raised`` or ``cancelled``: how the call
    ended, not whether the tool succeeded (the table's CHECK enforces the set).
    """
    if not await _table_available(db):
        return False
    await db.execute(
        "INSERT INTO client_tool_calls (client, server, tool, called_at, outcome, result_ref) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (client, server, tool, called_at, outcome, result_ref),
    )
    await db.commit()
    return True


async def prune(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    now: str | None = None,
) -> int:
    """Delete rows older than the window. Returns the count; 0 if the table is absent."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    if not await _table_available(db):
        return 0
    reference = datetime.fromisoformat(now) if now else datetime.now(UTC)
    cutoff = (reference - timedelta(days=older_than_days)).isoformat()
    cursor = await db.execute("DELETE FROM client_tool_calls WHERE called_at < ?", (cutoff,))
    await db.commit()
    return cursor.rowcount or 0
