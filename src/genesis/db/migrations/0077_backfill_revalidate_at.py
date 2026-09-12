"""Backfill ``ego_proposals.revalidate_at`` on pre-PR-6a pending rows.

Migration 0071 added the revalidation columns dark (no backfill) and the
PR-6a stamp in ``create_batch`` covers only rows created after it shipped —
so every pending proposal from before then has ``revalidate_at IS NULL`` and
is invisible to the reconcile stage's ⚠due premise-recheck flag. That gap is
how provably-dead proposals (a merged-PR review, a resolved backup incident)
sat on the board for a week with nothing prompting a re-check.

Data-only and idempotent: stamps ``revalidate_at = created_at + interval``
(the frozen per-urgency defaults below) and ``last_validated_at = created_at``
ONLY where NULL, on PENDING rows only. Informational eval rows (j9/gauntlet)
are deliberately excluded — per PR-6a's design they are acknowledge-only and
never revalidated.

Stamps are computed in Python (not SQLite ``datetime()``) for exact format
parity with the organic PR-6a stamps: the reconcile ⚠due check is a STRING
comparison against ``datetime.now(UTC).isoformat()``, and SQLite's
``YYYY-MM-DD HH:MM:SS`` output (space separator, no offset) would misorder
against ISO-8601 ``T``-separated values on same-day boundaries.

Frozen snapshots (self-contained by convention — migrations never import live
config/code whose meaning can drift):
- interval defaults mirror ``EgoConfig.revalidation_interval_hours``
- the informational tuple mirrors ``genesis.ego.types.INFORMATIONAL_ACTION_TYPES``

``down()`` is a no-op: reversing would require distinguishing backfilled
stamps from organic PR-6a stamps, which this migration deliberately makes
indistinguishable (both encode "created_at + interval").

(Numbered 0077: 0076 is the highest present; the runner applies by per-id
tracking and only duplicate prefixes are fatal.)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite

# Frozen snapshot of EgoConfig.revalidation_interval_hours defaults (hours).
_INTERVAL_HOURS = {"critical": 6, "high": 48, "normal": 72, "low": 168}

# Frozen snapshot of genesis.ego.types.INFORMATIONAL_ACTION_TYPES.
_INFORMATIONAL_ACTION_TYPES = ("j9_regression", "gauntlet_regression")


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM pragma_table_info('ego_proposals') WHERE name = 'revalidate_at'"
    )
    if await cursor.fetchone() is None:
        return  # pre-0071 DB — the column migration will run first next boot

    _info_placeholders = ",".join("?" for _ in _INFORMATIONAL_ACTION_TYPES)
    cursor = await db.execute(
        "SELECT id, created_at, urgency FROM ego_proposals "
        "WHERE status = 'pending' AND revalidate_at IS NULL "
        f"AND action_type NOT IN ({_info_placeholders})",
        _INFORMATIONAL_ACTION_TYPES,
    )
    rows = await cursor.fetchall()
    for row_id, created_at, urgency in rows:
        try:
            created_dt = datetime.fromisoformat(created_at)
        except (ValueError, TypeError):
            continue  # unparseable created_at — leave the row untouched
        hours = _INTERVAL_HOURS.get(urgency, _INTERVAL_HOURS["normal"])
        revalidate_at = (created_dt + timedelta(hours=hours)).isoformat()
        await db.execute(
            "UPDATE ego_proposals SET revalidate_at = ?, "
            "last_validated_at = COALESCE(last_validated_at, created_at) "
            "WHERE id = ? AND revalidate_at IS NULL",
            (revalidate_at, row_id),
        )


async def down(db: aiosqlite.Connection) -> None:
    """No-op: backfilled stamps are indistinguishable from organic ones."""
