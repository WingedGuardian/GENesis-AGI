"""CRUD for ``autonomous_email_sends`` — the WS-8 PR-D autonomous-send ledger.

One row per email the gate let through under a GRANTED capability cell (NOT an
owner-approved hold).  This is the keystone read/write layer for:

- the owner-visibility "Activity" dashboard tab (``list_recent``),
- the flag-as-bad correction (``mark_flagged`` → ``record_correction`` on the
  send's cell), and
- the per-cell rate-limit scope guard (``count_for_cell_since``),

because ``outreach_history`` carries no recipient / thread / cell column.
"""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    recipient: str,
    cell_domain: str,
    cell_verb: str,
    cell_risk_class: str,
    sent_at: str,
    outreach_id: str | None = None,
    thread_id: str | None = None,
    subject: str | None = None,
) -> str:
    """Record an autonomous send.  Best-effort ledger — callers must not let a
    failure here block or unwind an already-delivered email."""
    await db.execute(
        """INSERT INTO autonomous_email_sends
             (id, outreach_id, thread_id, recipient, subject,
              cell_domain, cell_verb, cell_risk_class, sent_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, outreach_id, thread_id, recipient, subject,
         cell_domain, cell_verb, cell_risk_class, sent_at),
    )
    await db.commit()
    return id


async def count_for_cell_since(
    db: aiosqlite.Connection,
    *,
    cell_domain: str,
    cell_verb: str,
    cell_risk_class: str,
    since: str,
) -> int:
    """Number of autonomous sends for this cell at/after ``since`` (ISO ts) —
    the per-cell rate-limit window."""
    cursor = await db.execute(
        """SELECT COUNT(*) FROM autonomous_email_sends
           WHERE cell_domain = ? AND cell_verb = ? AND cell_risk_class = ?
             AND sent_at >= ?""",
        (cell_domain, cell_verb, cell_risk_class, since),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM autonomous_email_sends WHERE id = ?", (id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_recent(db: aiosqlite.Connection, *, limit: int = 100) -> list[dict]:
    """Most-recent autonomous sends first — the Activity-tab action log."""
    cursor = await db.execute(
        "SELECT * FROM autonomous_email_sends ORDER BY sent_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def mark_flagged(db: aiosqlite.Connection, id: str, *, flagged_at: str) -> bool:
    """Mark a send as owner-flagged.  Atomic + idempotent: only the FIRST flag
    succeeds (``rowcount > 0``), so a double-click can't double-demote the cell.
    The caller records the correction only when this returns True."""
    cursor = await db.execute(
        "UPDATE autonomous_email_sends SET flagged_at = ? "
        "WHERE id = ? AND flagged_at IS NULL",
        (flagged_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0
