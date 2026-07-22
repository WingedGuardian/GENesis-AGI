"""d0007 — clear completed_at orphaned on non-terminal follow_ups rows.

``follow_ups.update_status`` stamped ``completed_at`` on a transition INTO a
terminal state but never cleared it on the way back OUT. So a row wrongly
flipped to ``completed`` (e.g. by a concurrent writer — the ego's
``resolve_follow_ups`` or another live session — during a bulk audit) and then
corrected back to ``in_progress``/``pending`` kept a stale ``completed_at``: a
timestamp that lies about a row that never actually completed, mis-keying any
GC/report window that reads ``completed_at`` directly (follow-up d67c83c7).

The code fix (``update_status`` now NULLs ``completed_at`` on every non-terminal
transition) stops NEW orphans; this migration heals the historical rows on
EVERY install (idempotent, post-boot) — the bug shipped in the code, so peer
installs carry orphans too, no per-install hand-fix.

Deterministic + tightly scoped: only rows where ``completed_at IS NOT NULL AND
status NOT IN ('completed','failed')``. A correctly-terminal row is never
touched, and a fresh install (no such rows) is a clean no-op. One bounded
UPDATE (orphans are a handful) — no per-row loop, so no batched commit needed.

``migrate()``/``verify()`` are SYNC (framework contract, cf. d0005/d0006) on
their own connections — never the runtime's async ``rt._db``.
"""

from __future__ import annotations

import logging
import sqlite3

from genesis.env import genesis_db_path

logger = logging.getLogger(__name__)

requires_operator = False

# A completed_at on a non-terminal row is the orphan signature. Terminal
# (completed/failed) rows legitimately carry completed_at and are left alone.
# The WHERE predicate is a fixed literal (identical in migrate/verify below).


def migrate() -> dict:
    """NULL completed_at on every non-terminal row that carries one."""
    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    try:
        cur = db.execute(
            "UPDATE follow_ups SET completed_at = NULL "
            "WHERE completed_at IS NOT NULL "
            "AND status NOT IN ('completed', 'failed')"
        )
        db.commit()
        cleared = cur.rowcount
    finally:
        db.close()
    logger.info("d0007: cleared completed_at on %d orphaned non-terminal follow_ups", cleared)
    return {"cleared": cleared}


def verify() -> bool:
    """Complete only when NO non-terminal row carries a completed_at.

    Read via ``mode=ro`` (WAL-aware) so it sees migrate()'s just-committed write
    — an ``immutable=1`` read would miss WAL-resident rows.
    """
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        (n,) = db.execute(
            "SELECT COUNT(*) FROM follow_ups "
            "WHERE completed_at IS NOT NULL "
            "AND status NOT IN ('completed', 'failed')"
        ).fetchone()
        return n == 0
    finally:
        db.close()
