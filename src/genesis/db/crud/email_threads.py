"""CRUD operations for email_threads and email_thread_messages tables."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


async def register_thread(
    db: aiosqlite.Connection,
    *,
    sent_message_id: str,
    recipient: str,
    owner: str = "outreach",
    owner_ref: str | None = None,
    subject: str | None = None,
    context: str | None = None,
    follow_up_days: int = 4,
) -> str:
    """Register a sent email for reply tracking. Returns thread id."""
    thread_id = _new_id()
    now = _now_iso()
    follow_up_after = (
        datetime.now(UTC) + timedelta(days=follow_up_days)
    ).isoformat()

    await db.execute(
        """INSERT INTO email_threads
           (id, sent_message_id, owner, owner_ref, recipient, subject,
            context, status, follow_up_after, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_reply', ?, ?, ?)""",
        (
            thread_id, sent_message_id, owner, owner_ref,
            recipient, subject, context, follow_up_after, now, now,
        ),
    )
    # Also record the sent message in thread_messages
    await db.execute(
        """INSERT OR IGNORE INTO email_thread_messages
           (thread_id, message_id, direction, sender, subject, body_preview, received_at)
           VALUES (?, ?, 'sent', ?, ?, NULL, ?)""",
        (thread_id, sent_message_id, recipient, subject, now),
    )
    await db.commit()
    return thread_id


async def match_reply(
    db: aiosqlite.Connection,
    *,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> dict | None:
    """Match an incoming email's In-Reply-To/References to a registered thread.

    Returns the thread dict if found, None otherwise.
    """
    candidates: list[str] = []
    if in_reply_to:
        candidates.append(in_reply_to)
    if references:
        candidates.extend(references)

    if not candidates:
        return None

    placeholders = ",".join("?" for _ in candidates)
    cursor = await db.execute(
        f"""SELECT * FROM email_threads
            WHERE sent_message_id IN ({placeholders})
            AND status != 'closed'
            ORDER BY created_at DESC LIMIT 1""",
        candidates,
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def record_reply(
    db: aiosqlite.Connection,
    *,
    thread_id: str,
    message_id: str,
    sender: str,
    subject: str | None = None,
    body_preview: str | None = None,
) -> None:
    """Record a received reply and update thread status."""
    now = _now_iso()
    await db.execute(
        """INSERT OR IGNORE INTO email_thread_messages
           (thread_id, message_id, direction, sender, subject, body_preview, received_at)
           VALUES (?, ?, 'received', ?, ?, ?, ?)""",
        (thread_id, message_id, sender, subject, body_preview, now),
    )
    await db.execute(
        "UPDATE email_threads SET status = 'replied', updated_at = ? WHERE id = ?",
        (now, thread_id),
    )
    await db.commit()


async def update_status(
    db: aiosqlite.Connection,
    thread_id: str,
    status: str,
) -> None:
    """Update thread status."""
    now = _now_iso()
    update_fields = "status = ?, updated_at = ?"
    params: list = [status, now]

    if status == "follow_up_sent":
        update_fields += ", follow_up_sent_at = ?"
        params.append(now)

    params.append(thread_id)
    await db.execute(
        f"UPDATE email_threads SET {update_fields} WHERE id = ?",
        params,
    )
    await db.commit()


async def get_stale_threads(
    db: aiosqlite.Connection,
) -> list[dict]:
    """Get threads awaiting reply past their follow-up deadline."""
    now = _now_iso()
    cursor = await db.execute(
        """SELECT * FROM email_threads
           WHERE status = 'awaiting_reply'
           AND follow_up_after IS NOT NULL
           AND follow_up_after <= ?
           ORDER BY follow_up_after ASC""",
        (now,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_thread_messages(
    db: aiosqlite.Connection,
    thread_id: str,
) -> list[dict]:
    """Get all messages in a thread, ordered chronologically."""
    cursor = await db.execute(
        """SELECT * FROM email_thread_messages
           WHERE thread_id = ?
           ORDER BY received_at ASC""",
        (thread_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_thread(
    db: aiosqlite.Connection,
    thread_id: str,
) -> dict | None:
    """Get a single thread by ID."""
    cursor = await db.execute(
        "SELECT * FROM email_threads WHERE id = ?", (thread_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_active_threads(
    db: aiosqlite.Connection,
) -> list[dict]:
    """List all non-closed threads."""
    cursor = await db.execute(
        """SELECT * FROM email_threads
           WHERE status != 'closed'
           ORDER BY updated_at DESC""",
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def has_inbound(db: aiosqlite.Connection, thread_id: str) -> bool:
    """True iff the thread has at least one received (inbound) message —
    i.e. a reply has come back, so a send on it is not cold outreach."""
    cursor = await db.execute(
        "SELECT 1 FROM email_thread_messages "
        "WHERE thread_id = ? AND direction = 'received' LIMIT 1",
        (thread_id,),
    )
    return await cursor.fetchone() is not None


async def recipient_in_thread(
    db: aiosqlite.Connection, thread_id: str, recipient: str,
) -> bool:
    """True iff ``recipient`` is a received-message sender in the thread.

    Deliberately does NOT treat a NULL/blank sender as a match: a single
    unparsed-sender row must not grant unbounded recipient scope (the safe
    failure for the SECURITY scope guard is to trip and hold, not wave
    through).
    """
    cursor = await db.execute(
        "SELECT 1 FROM email_thread_messages "
        "WHERE thread_id = ? AND direction = 'received' AND sender = ? LIMIT 1",
        (thread_id, recipient),
    )
    return await cursor.fetchone() is not None
