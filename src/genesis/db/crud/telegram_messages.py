"""CRUD operations for telegram_messages table.

Messages have a ``direction`` column ('inbound' for user messages, 'outbound'
for Genesis responses).  The UNIQUE constraint is (chat_id, message_id,
direction), so the same message_id can appear once per direction without
collision.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite


def _escape_like(query: str) -> str:
    """Escape SQLite LIKE wildcards (%, _) for literal matching."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def store(
    db: aiosqlite.Connection,
    *,
    chat_id: int,
    message_id: int,
    sender: str,
    content: str,
    thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    timestamp: str | None = None,
    direction: str = "inbound",
) -> None:
    """Store a Telegram message. Ignores duplicates (UNIQUE constraint)."""
    ts = timestamp or datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT OR IGNORE INTO telegram_messages
           (chat_id, message_id, thread_id, sender, content, timestamp,
            reply_to_message_id, direction)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (chat_id, message_id, thread_id, sender, content, ts,
         reply_to_message_id, direction),
    )
    await db.commit()


async def query_recent(
    db: aiosqlite.Connection,
    chat_id: int,
    *,
    thread_id: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return recent messages for a chat, newest first then reversed for readability."""
    if thread_id is not None:
        cursor = await db.execute(
            """SELECT * FROM telegram_messages
               WHERE chat_id = ? AND thread_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (chat_id, thread_id, limit),
        )
    else:
        cursor = await db.execute(
            """SELECT * FROM telegram_messages
               WHERE chat_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (chat_id, limit),
        )
    rows = await cursor.fetchall()
    # Return in chronological order (oldest first) for readability
    return [dict(r) for r in reversed(rows)]


async def query_all_recent(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[dict]:
    """Return recent messages across all chats, newest first then reversed."""
    cursor = await db.execute(
        """SELECT * FROM telegram_messages
           ORDER BY timestamp DESC LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in reversed(rows)]


async def search(
    db: aiosqlite.Connection,
    chat_id: int,
    query: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """Search messages by keyword (LIKE match, wildcards escaped)."""
    escaped = _escape_like(query)
    cursor = await db.execute(
        """SELECT * FROM telegram_messages
           WHERE chat_id = ? AND content LIKE ? ESCAPE '\\'
           ORDER BY timestamp DESC LIMIT ?""",
        (chat_id, f"%{escaped}%", limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in reversed(rows)]


async def search_all(
    db: aiosqlite.Connection,
    query: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """Search messages across all chats by keyword (LIKE match, wildcards escaped)."""
    escaped = _escape_like(query)
    cursor = await db.execute(
        """SELECT * FROM telegram_messages
           WHERE content LIKE ? ESCAPE '\\'
           ORDER BY timestamp DESC LIMIT ?""",
        (f"%{escaped}%", limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in reversed(rows)]
