"""CRUD for repo_pulse_runs/_annotations — the repo-pulse annotator store.

Session-manager PR-4a: rows record PR ↔ open-ledger-item matches made by the
detached ``scripts/repo_pulse_worker.py`` subprocess (short-lived RW conn,
WAL + busy_timeout). Read by the charter injector (proposals), the dashboard
Sessions tab (PR-4b), and the reconciliation sweep at the start of each run.

Only the EXACT tier (explicit ``Ledger: <id>`` marker) ever touches the live
``session_ledger`` — and that write goes through ``session_charters
.ledger_update``, not here. Everything in this module is annotation-side.

Subprocess writers do NOT run migrations, so writers guard on table existence
(cached per-process, TRUE result only — the capability_shadow pattern) and
no-op pre-migration. Migration 0062 is the sole schema authority; nothing
here creates tables.
"""

from __future__ import annotations

import aiosqlite

RUN_STATUSES = ("ok", "failed", "timeout", "lock_busy", "no_new_prs")
TIERS = ("exact", "fuzzy")
ANNOTATION_STATUSES = ("applied", "proposed", "confirmed", "rejected", "superseded")
# Terminal states a 'proposed' annotation may resolve to (reconciliation / 4b buttons).
RESOLUTION_STATUSES = ("confirmed", "rejected", "superseded")

# Per-process cache: only the TRUE result is cached — a missing table
# (pre-migration window) is re-checked every call so a subprocess writer
# self-heals the moment the server migration lands.
_tables_verified = False


async def _tables_available(db: aiosqlite.Connection) -> bool:
    global _tables_verified
    if _tables_verified:
        return True
    cursor = await db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('repo_pulse_runs', 'repo_pulse_annotations')"
    )
    row = await cursor.fetchone()
    exists = bool(row and row[0] == 2)
    if exists:
        _tables_verified = True
    return exists


async def record_run(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    started_at: str,
    finished_at: str | None,
    trigger: str,
    repo: str | None,
    cursor_before: str | None,
    cursor_after: str | None,
    status: str,
    n_prs: int = 0,
    n_open_items: int = 0,
    n_exact: int = 0,
    n_fuzzy: int = 0,
    latency_ms: int | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
    mode: str = "live",
    detail: str | None = None,
    annotations: list[dict] | None = None,
) -> bool:
    """Insert one run row + its annotation rows in a single transaction.

    Returns False (no-op) if the tables don't exist yet (subprocess
    pre-migration window) — the caller must then NOT advance its cursor, so
    the enumeration window is preserved until the migration lands.

    Annotation inserts use INSERT OR IGNORE against the
    ``(tier, item_id, pr_number)`` unique index: a re-covered enumeration
    window re-observing the same match is silently absorbed, never
    duplicated. Annotation dicts carry the _annotations columns minus
    run_id (stamped from the run here); ``id`` and ``observed_at`` are
    per-annotation.
    """
    if status not in RUN_STATUSES:
        raise ValueError(f"invalid run status: {status!r}")
    # Validate EVERYTHING before the first INSERT: a mid-transaction raise
    # would strand an uncommitted run row on the connection (and shared
    # connections are never rolled back here by house rule).
    for ann in annotations or []:
        if ann.get("tier") not in TIERS:
            raise ValueError(f"invalid annotation tier: {ann.get('tier')!r}")
        if ann.get("status") not in ANNOTATION_STATUSES:
            raise ValueError(f"invalid annotation status: {ann.get('status')!r}")
        if not ann.get("item_id"):
            raise ValueError("annotation missing item_id")
        if not isinstance(ann.get("pr_number"), int):
            raise ValueError(f"annotation pr_number not an int: {ann.get('pr_number')!r}")
    if not await _tables_available(db):
        return False
    await db.execute(
        "INSERT INTO repo_pulse_runs "
        "(run_id, started_at, finished_at, trigger, repo, cursor_before, "
        "cursor_after, status, n_prs, n_open_items, n_exact, n_fuzzy, "
        "latency_ms, prompt_version, model, mode, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            started_at,
            finished_at,
            trigger,
            repo,
            cursor_before,
            cursor_after,
            status,
            n_prs,
            n_open_items,
            n_exact,
            n_fuzzy,
            latency_ms,
            prompt_version,
            model,
            mode,
            detail,
        ),
    )
    for ann in annotations or []:
        await db.execute(
            "INSERT OR IGNORE INTO repo_pulse_annotations "
            "(id, run_id, observed_at, tier, item_id, item_session_id, "
            "item_text, pr_number, pr_title, pr_merged_at, confidence, "
            "rationale, status, resolved_at, resolution_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ann["id"],
                run_id,
                ann["observed_at"],
                ann["tier"],
                ann["item_id"],
                ann.get("item_session_id"),
                ann.get("item_text"),
                ann["pr_number"],
                ann.get("pr_title"),
                ann.get("pr_merged_at"),
                ann.get("confidence"),
                ann.get("rationale"),
                ann["status"],
                ann.get("resolved_at"),
                ann.get("resolution_ref"),
            ),
        )
    await db.commit()
    return True


async def annotation_exists(
    db: aiosqlite.Connection, tier: str, item_id: str, pr_number: int
) -> bool:
    """True when this (tier, item_id, pr_number) match was already recorded.

    The worker's re-absorb guard: a re-covered enumeration window (or a
    deliberately reopened item) must not be acted on twice for the same
    PR. Any status counts — a rejected proposal is still a decision made.
    """
    if tier not in TIERS:
        raise ValueError(f"invalid annotation tier: {tier!r}")
    if not await _tables_available(db):
        return False
    cursor = await db.execute(
        "SELECT 1 FROM repo_pulse_annotations "
        "WHERE tier = ? AND item_id = ? AND pr_number = ? LIMIT 1",
        (tier, item_id, pr_number),
    )
    return await cursor.fetchone() is not None


async def resolve_annotation(
    db: aiosqlite.Connection,
    annotation_id: str,
    *,
    status: str,
    resolved_at: str,
    resolution_ref: str | None = None,
) -> bool:
    """Resolve a 'proposed' annotation to a terminal status.

    Only annotations currently in 'proposed' are eligible — applied rows are
    exact-tier facts and terminal rows never flip (so reconciliation and the
    dashboard buttons can't fight). Returns True iff a row changed.
    """
    if status not in RESOLUTION_STATUSES:
        raise ValueError(f"invalid resolution status: {status!r}")
    if not await _tables_available(db):
        return False
    cursor = await db.execute(
        "UPDATE repo_pulse_annotations SET status = ?, resolved_at = ?, "
        "resolution_ref = ? WHERE id = ? AND status = 'proposed'",
        (status, resolved_at, resolution_ref, annotation_id),
    )
    await db.commit()
    return bool(cursor.rowcount)


async def list_runs(db: aiosqlite.Connection, *, limit: int = 200) -> list[dict]:
    """Runs newest first. Assumes a Row factory."""
    lim = max(1, min(int(limit), 1000))
    cursor = await db.execute(
        "SELECT * FROM repo_pulse_runs ORDER BY started_at DESC LIMIT ?", (lim,)
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_annotations(
    db: aiosqlite.Connection,
    *,
    session_id: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Annotations newest first, optionally filtered by session and/or status."""
    if status is not None and status not in ANNOTATION_STATUSES:
        raise ValueError(f"invalid annotation status: {status!r}")
    lim = max(1, min(int(limit), 2000))
    clauses, params = [], []
    if session_id is not None:
        clauses.append("item_session_id = ?")
        params.append(session_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    cursor = await db.execute(
        f"SELECT * FROM repo_pulse_annotations {where}"  # noqa: S608 — clauses are literals
        "ORDER BY observed_at DESC LIMIT ?",
        (*params, lim),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def summary(db: aiosqlite.Connection) -> dict:
    """Counts-only health rollup: run status histogram + annotation
    tier/status matrix + fuzzy precision (confirmed / (confirmed+rejected))."""
    out: dict = {"runs": {}, "annotations": {}, "precision": None}
    if not await _tables_available(db):
        return out
    cursor = await db.execute("SELECT status, COUNT(*) AS n FROM repo_pulse_runs GROUP BY status")
    for row in await cursor.fetchall():
        out["runs"][row[0]] = row[1]
    cursor = await db.execute(
        "SELECT tier, status, COUNT(*) AS n FROM repo_pulse_annotations GROUP BY tier, status"
    )
    for row in await cursor.fetchall():
        out["annotations"][f"{row[0]}/{row[1]}"] = row[2]
    confirmed = out["annotations"].get("fuzzy/confirmed", 0)
    rejected = out["annotations"].get("fuzzy/rejected", 0)
    if confirmed + rejected:
        out["precision"] = round(confirmed / (confirmed + rejected), 3)
    return out


async def prune_repo_pulse(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 45,
    now: str,
) -> int:
    """Delete runs+annotations older than *older_than_days* relative to ISO ``now``.

    Retention for the unbounded pulse store (wired into disk_hygiene.sh),
    mirroring ``prune_session_ledger_shadow``. ``now`` is injected (never
    wall-clock here) so the cutover is deterministic and testable. No-ops
    before migration 0062; never creates tables. Returns rows deleted.
    """
    if not await _tables_available(db):
        return 0
    cutoff = _iso_days_before(now, older_than_days)
    deleted = 0
    cursor = await db.execute("DELETE FROM repo_pulse_annotations WHERE observed_at < ?", (cutoff,))
    deleted += cursor.rowcount or 0
    cursor = await db.execute("DELETE FROM repo_pulse_runs WHERE started_at < ?", (cutoff,))
    deleted += cursor.rowcount or 0
    await db.commit()
    return deleted


def _iso_days_before(now_iso: str, days: int) -> str:
    """Return the ISO8601 timestamp *days* before ``now_iso``."""
    from datetime import datetime, timedelta

    dt = datetime.fromisoformat(now_iso)
    return (dt - timedelta(days=days)).isoformat()
