"""CRUD for pr_verifications — per-merged-PR post-merge verification obligations.

Issue #1718 half B. One row per merged PR, written ONLY by the repo-pulse
worker's verification lane (``repo_pulse_worker._verification_lane``): a
docs-only diff arrives already ``closed`` with the deterministic-exemption
reason; everything else arrives ``open`` and stays open until the validator
session records evidence via :func:`close_verification`. Read by the worker
CLI's ``--verification-backlog`` (the day-one reader) and, later, the Wave-3
validator.

Why its own table rather than ``follow_ups`` is recorded once, where the store
is born: the ``20260906234824_pr_verifications`` migration docstring. Short
version: follow_ups' readers (ego dispatch, morning report) surface rows as
actionable work, and these are a ledger, not work.

Subprocess writers do NOT run migrations, so writers guard on table existence
pattern) and no-op pre-migration. The migration + ``schema/_tables.py`` are
the schema authority; nothing here creates tables. ``now`` is always injected
(never wall-clock here) so behaviour is deterministic and testable.
"""

from __future__ import annotations

import uuid

import aiosqlite

STATUSES = ("open", "closed")

# Per-CONNECTION-TARGET cache: only the TRUE result is cached — a missing table
# (pre-migration window) is re-checked every call so a subprocess writer
# self-heals the moment the server migration lands.
#
# Keyed by the connection's DB path rather than a bare module flag (the sibling
# repo_pulse crud's shape): a process-global TRUE cached against one database
# answers for every database that process later opens, and the lie surfaces as
# an OperationalError inside the lane's try/except — a silent incomplete run.
# Today's entry points touch one DB per process, so this is prophylactic; it
# costs one dict lookup and removes a trap rather than documenting it.


async def _tables_available(db: aiosqlite.Connection) -> bool:
    """Does the table exist? Asked EVERY time, deliberately uncached.

    There was a per-path cache here and it never populated: `_db_key` read
    `_conn_path` / `_path` off the connection, and MEASURED against the
    installed aiosqlite (0.22.1) neither attribute exists — so the key was
    always None, nothing was ever added, and the module docstring's claim of a
    cache was false in every execution (audit, PR #1836). The version before
    that fell back to `id(db)`, which DID cache and was worse: CPython reuses
    the id of a closed object, so a later connection to a DIFFERENT database
    could be told the tables were present without consulting `sqlite_master`.

    Rather than key it on something that works, the mechanism is gone. It was
    never load-bearing — it saved one `sqlite_master` COUNT on a short-lived
    worker connection — and a cache that has demonstrably been wrong in both of
    its implementations is not worth a third attempt. Correctness is unchanged:
    both prior versions failed toward always checking, which is what this does.
    """
    cursor = await db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = 'pr_verifications'"
    )
    row = await cursor.fetchone()
    return bool(row and row[0] == 1)


async def tables_available(db: aiosqlite.Connection) -> bool:
    """Public existence check (see repo_pulse.tables_available for the pattern)."""
    return await _tables_available(db)


async def exists(db: aiosqlite.Connection, *, repo: str, pr_number: int) -> bool:
    """True when this merged PR already has a verification row, in ANY status.

    The lane's cheap pre-check: an already-recorded PR must not cost a
    changed-files API call on window re-coverage. False pre-migration —
    the caller then no-ops via :func:`open_verification` anyway.
    """
    if not await _tables_available(db):
        return False
    cursor = await db.execute(
        "SELECT 1 FROM pr_verifications WHERE repo = ? AND pr_number = ? LIMIT 1",
        (repo, pr_number),
    )
    return await cursor.fetchone() is not None


async def open_verification(
    db: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    pr_title: str | None,
    merged_at: str,
    now: str,
    closed_reason: str | None = None,
    commit: bool = True,
) -> str:
    """Record one merged PR's verification obligation. Returns an outcome word.

    ``closed_reason`` set → the row is born CLOSED (the deterministic docs-only
    exemption; ``closed_at`` = ``now``). Otherwise it is born ``open``.

    INSERT OR IGNORE against the (repo, pr_number) unique index — the schema is
    the dedup, so a concurrent writer or a re-covered window can never
    duplicate; the precheck in :func:`exists` is an API-cost optimization, not
    the guard. Outcomes, explicit rather than a tri-state bool:

    - ``"created"`` — the row landed.
    - ``"exists"``  — a row for this (repo, pr_number) already existed; nothing
      changed (whatever its status — a closed row is a decision already made).
    - ``"unavailable"`` — pre-migration window; nothing written. The caller
      must NOT count this PR as recorded (dedup makes the retry idempotent).
    """
    if not await _tables_available(db):
        return "unavailable"
    status = "closed" if closed_reason else "open"
    cursor = await db.execute(
        "INSERT OR IGNORE INTO pr_verifications "
        "(id, repo, pr_number, pr_title, merged_at, status, closed_reason, "
        "closed_at, evidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            uuid.uuid4().hex,
            repo,
            pr_number,
            pr_title,
            merged_at,
            status,
            closed_reason,
            now if closed_reason else None,
            now,
        ),
    )
    if commit:
        await db.commit()
    return "created" if cursor.rowcount else "exists"


async def close_verification(
    db: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    reason: str,
    evidence: str | None,
    now: str,
) -> bool:
    """Close an OPEN obligation with its verification record (the validator's
    write). True iff a row changed — a closed row never flips back or gets its
    reason overwritten, so two validators cannot fight over one PR."""
    if not reason or not reason.strip():
        # A closed row with no reason is an unverifiable claim in permanent
        # record — the row would say "handled" and carry nothing. The schema
        # permits it; this writer does not, because the eventual closer is a
        # validator SESSION (an LLM caller), which is exactly the caller that
        # would pass an empty string. Raise rather than return False: a caller
        # that omitted its reason has a bug, not a no-op.
        raise ValueError("close_verification requires a non-empty reason")
    if not await _tables_available(db):
        return False
    cursor = await db.execute(
        "UPDATE pr_verifications SET status = 'closed', closed_reason = ?, "
        "closed_at = ?, evidence = ? "
        "WHERE repo = ? AND pr_number = ? AND status = 'open'",
        (reason, now, evidence, repo, pr_number),
    )
    await db.commit()
    return bool(cursor.rowcount)


async def list_open(db: aiosqlite.Connection, *, limit: int = 500) -> list[dict]:
    """Open obligations, OLDEST merge first — the backlog reader.

    Oldest-first because the backlog's point is what has waited longest.
    Assumes a Row factory. Empty pre-migration.
    """
    if not await _tables_available(db):
        return []
    lim = max(1, min(int(limit), 2000))
    cursor = await db.execute(
        "SELECT * FROM pr_verifications WHERE status = 'open' ORDER BY merged_at ASC LIMIT ?",
        (lim,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def counts(db: aiosqlite.Connection) -> dict:
    """Status histogram, e.g. ``{"open": 12, "closed": 40}``. Empty pre-migration."""
    out: dict = {}
    if not await _tables_available(db):
        return out
    cursor = await db.execute("SELECT status, COUNT(*) FROM pr_verifications GROUP BY status")
    for row in await cursor.fetchall():
        out[row[0]] = row[1]
    return out


async def prune_closed(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 180,
    now: str,
) -> int:
    """Delete CLOSED rows older than *older_than_days* (by closed_at). Retention
    for the unbounded store (wired into scripts/prune_repo_pulse.py → the
    disk-hygiene timer).

    OPEN rows are never pruned — an open row IS the obligation, and deleting it
    would silently forgive an unverified merge. The 180-day window on closed
    rows keeps recent gap-detection ("no row within the retention window means
    the lane never recorded that PR" — the lane fails its run rather than
    advancing the cursor past PRs it could not record, so a gap is a signal
    rather than routine) while bounding growth at ~11 merges/day ≈ 2k retained closed rows; flagged
    as a reviewable number, not derived from a hard budget. ``now`` injected.

    Rejects a sub-1-day retention window, mirroring ``prune_merge_journal``
    (crud/entities.py) which names this exact class: with ``older_than_days <= 0``
    the cutoff lands at or in the FUTURE relative to ``now`` (subtracting a
    negative pushes it forward), so ``closed_at < cutoff`` would match EVERY
    closed row. The guard lives HERE rather than only at the CLI because the
    caller that gets it wrong is the one that never thought about it — the CLI
    validates too, for a readable error instead of a traceback.
    """
    if older_than_days < 1:
        raise ValueError(
            f"prune_closed: retention window must be >= 1 day, got "
            f"{older_than_days!r}; a sub-1 window sets the cutoff at/after now and "
            f"would delete EVERY closed verification row, destroying the "
            f"gap-detection window that makes a missing row a signal."
        )
    if not await _tables_available(db):
        return 0
    from datetime import datetime, timedelta

    cutoff = (datetime.fromisoformat(now) - timedelta(days=older_than_days)).isoformat()
    cursor = await db.execute(
        "DELETE FROM pr_verifications WHERE status = 'closed' AND closed_at < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount or 0
