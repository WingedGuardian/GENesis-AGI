"""Create ``pending_email_sends`` — the WS-8 email autonomy gate hold store.

When the deterministic autonomy gate (in ``outreach.pipeline._deliver``, for
``channel=="email"``) holds an outbound email whose capability cell isn't
GRANTED, it records the fully-resolved send here (preserving the
``validated_recipient`` that ``_defer`` would lose) and creates a linked
``approval_requests`` row.  A periodic resolution watcher then sends below the
gate on approval, or expires the hold on rejection/timeout.

``request_id`` is UNIQUE — the schema-level double-send guard: even if the
watcher fires twice, only one row per approval can transition out of 'held'.

Idempotent (``IF NOT EXISTS``).  Fresh installs get the same DDL via
``db/schema/_tables.py``; this migration covers existing installs.
"""

from __future__ import annotations

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS pending_email_sends (
        id                  TEXT PRIMARY KEY,
        request_id          TEXT NOT NULL UNIQUE,   -- FK approval_requests.id; double-send guard
        thread_id           TEXT,
        validated_recipient TEXT NOT NULL,
        channel             TEXT NOT NULL DEFAULT 'email',
        category            TEXT NOT NULL,
        message             TEXT NOT NULL,
        cell_domain         TEXT NOT NULL,
        cell_verb           TEXT NOT NULL,
        cell_risk_class     TEXT NOT NULL,
        held_at             TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'held'
                                CHECK (status IN ('held', 'sent', 'rejected', 'expired')),
        sent_at             TEXT,
        rejected_at         TEXT
    )
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_pending_email_sends_status "
    "ON pending_email_sends(status)",
)


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (and its indexes) — development/testing only."""
    await db.execute("DROP TABLE IF EXISTS pending_email_sends")
