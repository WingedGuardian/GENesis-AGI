"""CRUD operations for pending_outreach table.

Foreground CC sessions write here; the bridge drains it via the outreach pipeline.
"""

from __future__ import annotations

import logging
import uuid

import aiosqlite

logger = logging.getLogger(__name__)


async def enqueue(
    db: aiosqlite.Connection,
    *,
    message: str,
    category: str,
    channel: str = "telegram",
    urgency: str = "low",
    deliver_after: str | None = None,
    thread_id: str | None = None,
    validated_recipient: str | None = None,
    labeled_surplus: bool = False,
) -> str:
    """Queue a message for bridge delivery. Returns the pending ID.

    ``thread_id`` / ``validated_recipient`` carry the resolved email thread and
    recipient through the queue so the genesis-server drain can rebuild a
    properly-routed request — without them a queued email defaulted to the
    agent's own address (a self-send loop).

    ``labeled_surplus`` carries the BULK/campaign flag through the queue so a
    QUEUED cold-marketing send (the MCP subprocess enqueue path) is rebuilt with
    ``labeled_surplus=True`` and classifies BULK at the autonomy gate — without it
    a queued cold send mis-classifies as IDENTITY.
    """
    from datetime import UTC, datetime

    pending_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO pending_outreach
           (id, message, category, channel, urgency, deliver_after, created_at,
            thread_id, validated_recipient, labeled_surplus)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pending_id,
            message,
            category,
            channel,
            urgency,
            deliver_after,
            now,
            thread_id,
            validated_recipient,
            1 if labeled_surplus else 0,
        ),
    )
    await db.commit()
    return pending_id


async def drain(
    db: aiosqlite.Connection,
    *,
    now: str,
) -> list[dict]:
    """Fetch undelivered messages ready for delivery (max 20 per cycle).

    Exposes the always-present ``rowid`` alongside the columns so a row whose
    ``id`` is NULL (legacy rows inserted before ``enqueue`` set a uuid) can
    still be marked delivered by rowid — otherwise ``mark_delivered`` matches
    ``WHERE id = NULL`` (zero rows), the row never clears, and it is
    re-drained every cycle forever (a churn/log-noise loop the 24h age-out
    could not break because the age-out drop *is* a ``mark_delivered`` call).
    """
    cursor = await db.execute(
        """SELECT rowid AS rowid, * FROM pending_outreach
           WHERE delivered = 0
             AND cancelled_at IS NULL
             AND (deliver_after IS NULL OR deliver_after <= ?)
           ORDER BY created_at ASC
           LIMIT 20""",
        (now,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def cancel(db: aiosqlite.Connection, pending_id: str) -> tuple[bool, str]:
    """Cancel a still-queued message. Returns (cancelled, reason).

    Exists so that changing a queued message (a reminder whose time moved, a
    notification made obsolete by events) does not require either sending a
    duplicate or calling :func:`mark_delivered` on the original. That second
    option is the one worth naming: it writes ``delivered = 1`` for a message
    that was never sent, into a table ``outreach_queue`` reads back, so a later
    session sees it as delivered and concludes the recipient was told something
    they were not. Cancellation is a distinct state precisely so it cannot be
    mistaken for delivery.

    Never raises. The narrow WHERE clause is what makes the first element mean
    "I cancelled it" rather than "the row exists": a caller can act on True.

    The reason distinguishes the three ways a cancel can find nothing to cancel,
    and one of them must NOT be described as delivery. ``delivered = 1`` in this
    table means DEQUEUED, not sent: the drain writes it for ``HELD`` (handed to
    the autonomy gate's approval queue, and the message still sends once the
    owner approves), for ``IGNORED``, and for the 24h age-out whose own log line
    says "never delivered" — see ``outreach/scheduler.py`` around the terminal-
    disposition branch. So "already delivered, cannot be un-sent" would be a
    false statement in exactly the window where retraction matters most: a gated
    message waiting on approval. ``already_dequeued`` is the honest name.

    Making ``delivered`` stop meaning two things is a larger fix in the scheduler
    and is tracked separately; this function refuses to assert what it cannot
    know.
    """
    from datetime import UTC, datetime

    cursor = await db.execute(
        """UPDATE pending_outreach
              SET cancelled_at = ?
            WHERE id = ?
              AND delivered = 0
              AND cancelled_at IS NULL""",
        (datetime.now(UTC).isoformat(), pending_id),
    )
    await db.commit()
    if cursor.rowcount > 0:
        return True, "cancelled"

    # Positional, not by name: this module's other readers happen to run on a
    # Row-factory connection, but cancel() must not depend on one being set.
    row_cursor = await db.execute(
        "SELECT cancelled_at FROM pending_outreach WHERE id = ?",
        (pending_id,),
    )
    row = await row_cursor.fetchone()
    if row is None:
        return False, "unknown_id"
    # Ordering matters: a row can be BOTH cancelled and later dequeued if a cancel
    # lost a race with the send loop. Reporting already_cancelled there is the
    # accurate answer to "did my cancel take effect" — it did, on the row.
    if row[0] is not None:
        return False, "already_cancelled"
    return False, "already_dequeued"


async def mark_delivered(
    db: aiosqlite.Connection,
    pending_id: str,
    *,
    delivered_at: str,
) -> bool:
    """Mark a pending message as delivered by its ``id`` primary key."""
    cursor = await db.execute(
        "UPDATE pending_outreach SET delivered = 1, delivered_at = ? WHERE id = ?",
        (delivered_at, pending_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_delivered_by_rowid(
    db: aiosqlite.Connection,
    rowid: int,
    *,
    delivered_at: str,
) -> bool:
    """Mark a pending message as delivered by ``rowid``.

    Fallback for rows with a NULL ``id`` (see ``drain``). ``rowid`` is always
    present and unique, so this always targets exactly the intended row.
    """
    cursor = await db.execute(
        "UPDATE pending_outreach SET delivered = 1, delivered_at = ? WHERE rowid = ?",
        (delivered_at, rowid),
    )
    await db.commit()
    return cursor.rowcount > 0


async def ensure_table(db: aiosqlite.Connection) -> None:
    """Create the pending_outreach table if it doesn't exist.

    Called by the MCP standalone fallback to ensure the table exists
    without requiring a full bootstrap.
    """
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending_outreach (
            id                  TEXT PRIMARY KEY,
            message             TEXT NOT NULL,
            category            TEXT NOT NULL,
            channel             TEXT NOT NULL DEFAULT 'telegram',
            urgency             TEXT NOT NULL DEFAULT 'low',
            deliver_after       TEXT,
            created_at          TEXT NOT NULL,
            delivered           INTEGER NOT NULL DEFAULT 0,
            delivered_at        TEXT,
            thread_id           TEXT,
            validated_recipient TEXT,
            labeled_surplus     INTEGER NOT NULL DEFAULT 0,
            cancelled_at        TEXT
        )
    """)
    # Legacy DBs whose pending_outreach predates labeled_surplus: add it here too
    # (this standalone-fallback path runs before the numbered migration on a
    # subprocess-only boot). Guarded so a re-run / fresh DB never errors.
    # One try PER column. They used to share a single `suppress(Exception)`, so a
    # transient failure on the first ALTER (SQLITE_LOCKED on DDL is real enough
    # that the migration runner carries a retry loop for it) silently skipped the
    # second — and `cancelled_at` missing is not a degraded flag, it breaks
    # `drain`'s WHERE clause and stops the whole queue delivering.
    import contextlib

    cols: set[str] = set()
    with contextlib.suppress(Exception):
        cursor = await db.execute("PRAGMA table_info(pending_outreach)")
        cols = {row[1] for row in await cursor.fetchall()}

    for column, ddl in (
        (
            "labeled_surplus",
            "ALTER TABLE pending_outreach ADD COLUMN labeled_surplus INTEGER NOT NULL DEFAULT 0",
        ),
        ("cancelled_at", "ALTER TABLE pending_outreach ADD COLUMN cancelled_at TEXT"),
    ):
        if column in cols:
            continue
        try:
            await db.execute(ddl)
        except Exception as exc:  # noqa: BLE001 — one column must not block the next
            # A duplicate column is the expected no-op when the PRAGMA read above
            # failed; anything else is real and must not vanish.
            if "duplicate column" not in str(exc).lower():
                logger.warning("pending_outreach: ALTER adding %s failed: %s", column, exc)
    await db.commit()
