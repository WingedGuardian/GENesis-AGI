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
    """``(completed_at, new_commit)`` of the most recent successful update whose
    DEPLOYED COMMIT ACTUALLY ADVANCED versus the previous successful update.

    Naively filtering ``new_commit != old_commit`` WITHIN a single row is
    insufficient. ``update.sh`` captures ``old_commit`` as HEAD *before* its own
    ``git pull`` and ``new_commit`` as HEAD *after* (update.sh:178/1199). If the
    repo was advanced by a MANUAL ``git pull`` *before* ``update.sh`` ran, its own
    pull is a no-op, so it records ``old_commit == new_commit`` even though the
    deployed commit genuinely changed versus the prior deploy — and a session that
    started before that activation is running older MCP code but would be missed.
    (Observed in real history: an ``addc6366==addc6366`` success row that followed
    a ``1147359b`` baseline.)

    So a row is "code-changing" iff its ``new_commit`` differs from the PREVIOUS
    successful row's ``new_commit`` (``LAG``), falling back to the in-row
    ``old_commit`` for the first-ever row (no predecessor): a true no-op re-run
    (``new_commit`` unchanged from the prior deploy) is still ignored, and a lone
    first ``x==x`` row is still treated as no-change. Both columns are ``NOT NULL``
    (migration 0024); ``status = 'success'`` excludes rollbacks.

    Ordered by ``datetime(completed_at)`` (true-instant, mixed-offset-safe).
    ``None`` when no code-changing success row exists (fresh install, or only
    no-op re-runs) → callers treat that as "nothing can be stale".

    Residual (documented, benign): a rollback/restore that reverts the code
    WITHOUT writing a new success row leaves the prior code-changing row
    governing, so procs that predate it stay shown stale even though disk now
    matches them again — a cosmetic over-report that self-corrects on the next
    real deploy or session restart. This reader never blocks anything.
    """
    cur = await db.execute(
        "SELECT completed_at, new_commit FROM ("
        "  SELECT completed_at, new_commit, old_commit,"
        "         LAG(new_commit) OVER (ORDER BY datetime(completed_at)) AS prev_commit"
        "  FROM update_history WHERE status = 'success'"
        ") WHERE new_commit != COALESCE(prev_commit, old_commit) "
        "ORDER BY datetime(completed_at) DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return None
    return str(row[0]), str(row[1] or "")
