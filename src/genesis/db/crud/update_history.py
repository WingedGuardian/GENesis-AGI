"""CRUD reader for the ``update_history`` ledger (Genesis self-update attempts).

Writes are owned by ``scripts/update.sh`` (the deploy script records each run);
this module provides read access through the CRUD layer so callers never issue
raw SQL against ``genesis.db``.
"""

from __future__ import annotations

import aiosqlite


async def last_successful_deploy_commit(db: aiosqlite.Connection) -> str | None:
    """Return ``new_commit`` of the most recent successful update, else None.

    Ordered by ``datetime(started_at)`` so ISO timestamps with differing
    timezone offsets compare by true instant rather than lexicographically.
    """
    cur = await db.execute(
        "SELECT new_commit FROM update_history "
        "WHERE status = 'success' AND new_commit IS NOT NULL "
        "ORDER BY datetime(started_at) DESC LIMIT 1",
    )
    row = await cur.fetchone()
    return str(row[0]).strip() if row and row[0] else None


async def last_successful_update(db: aiosqlite.Connection) -> tuple[str, str] | None:
    """``(completed_at, new_commit)`` of the most recent successful update.

    None when the table is empty (pre-first-update install). Ordered by
    ``datetime(completed_at)`` — the deploy-staleness age axis cares about
    when the update FINISHED, and datetime() compares mixed-offset ISO
    timestamps by true instant."""
    cur = await db.execute(
        "SELECT completed_at, new_commit FROM update_history "
        "WHERE status = 'success' ORDER BY datetime(completed_at) DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return None
    return str(row[0]), str(row[1] or "")


async def last_code_changing_update(db: aiosqlite.Connection) -> tuple[str, str] | None:
    """``(completed_at, new_commit)`` of the most recent successful update that
    ACTUALLY CHANGED THE CODE (``new_commit != old_commit``).

    A no-op ``update.sh`` re-run (already at the latest commit) still writes a
    ``success`` row with ``new_commit == old_commit``; those must not count as a
    fresh deploy, or a session that started before such a re-run would be shown
    stale despite running identical code. Both columns are ``NOT NULL`` (migration
    0024), so the inequality is NULL-safe; ``status = 'success'`` already excludes
    ``rolled_back``/``conflicts_pending`` rows.

    Ordered by ``datetime(completed_at)`` (true-instant, mixed-offset-safe).
    ``None`` when no code-changing success row exists (fresh install, or only
    no-op re-runs recorded) → callers treat that as "nothing can be stale".

    Residual (documented, benign): a rollback/restore that reverts the code
    WITHOUT writing a new success row leaves the prior code-changing row
    governing, so procs that predate it stay shown stale even though disk now
    matches them again — a cosmetic over-report that self-corrects on the next
    real deploy or session restart. This reader never blocks anything.
    """
    cur = await db.execute(
        "SELECT completed_at, new_commit FROM update_history "
        "WHERE status = 'success' AND new_commit != old_commit "
        "ORDER BY datetime(completed_at) DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return None
    return str(row[0]), str(row[1] or "")
