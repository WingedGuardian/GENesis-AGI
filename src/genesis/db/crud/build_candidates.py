"""CRUD for build_candidates — the capability-build lane's decision ledger.

One row per (notepad item, verdict episode). The row carries the verdict and
its reasoning, the pinned build spec, the greenlight approval reference, the
user's actual decision (calibration ground truth), and the build outcome.

Written only by the genesis-server process (BuildLane + executor delivery),
so unlike the shadow stores there is no subprocess-writer table guard;
migration 0047 / ``_tables.py`` are the schema authority.
"""

from __future__ import annotations

import aiosqlite

VERDICTS = ("build", "dont_build", "needs_discussion")
USER_DECISIONS = ("approved", "rejected", "discussed")
OUTCOMES = (
    "pending", "submitted", "built", "pr_opened",
    "scope_blocked", "build_failed", "abandoned",
)


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    item_key: str,
    item_title: str,
    source_file: str,
    verdict: str,
    batch_id: str | None = None,
    eval_path: str | None = None,
    verdict_reason: str | None = None,
    confidence: str | None = None,
    build_spec: str | None = None,
    plan_path: str | None = None,
    approval_request_id: str | None = None,
    created_at: str | None = None,
) -> str:
    """Insert a new candidate row (outcome starts at 'pending').

    ``approval_request_id`` is set at insert time for ``build`` candidates
    (the greenlight card is sent first, then the row records its request id)
    so a crash between card and row simply re-cards idempotently on the next
    eval rather than stranding a carded-but-unrecorded item.

    Raises ``aiosqlite.IntegrityError`` if an OPEN candidate for the same
    ``item_key`` already exists (partial unique index) — callers treat that
    as "card already pending, do not re-ask".
    """
    await db.execute(
        """INSERT INTO build_candidates
           (id, item_key, item_title, source_file, batch_id, eval_path,
            verdict, verdict_reason, confidence, build_spec, plan_path,
            approval_request_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   COALESCE(?, datetime('now')), COALESCE(?, datetime('now')))""",
        (id, item_key, item_title, source_file, batch_id, eval_path,
         verdict, verdict_reason, confidence, build_spec, plan_path,
         approval_request_id, created_at, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    """Fetch one candidate row by primary key."""
    cursor = await db.execute(
        "SELECT * FROM build_candidates WHERE id = ?", (id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_open_by_item_key(
    db: aiosqlite.Connection, item_key: str
) -> dict | None:
    """Return the single OPEN (undecided) candidate for *item_key*, if any."""
    cursor = await db.execute(
        "SELECT * FROM build_candidates "
        "WHERE item_key = ? AND user_decision IS NULL",
        (item_key,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_any_by_item_key(
    db: aiosqlite.Connection, item_key: str
) -> dict | None:
    """Return the most recent candidate for *item_key*, ANY decision state.

    The partial unique index only guards OPEN (undecided) rows, so it does
    not stop a *decided* item from being re-inserted. A capability-notepad
    item stays in the file after it is built and is re-evaluated on every
    rescan — this is the permanent dedup axis that prevents re-carding an
    already-adjudicated item. A genuine title/URL edit changes the item_key
    and legitimately produces a fresh candidate.
    """
    cursor = await db.execute(
        "SELECT * FROM build_candidates "
        "WHERE item_key = ? ORDER BY created_at DESC LIMIT 1",
        (item_key,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_approval_request(
    db: aiosqlite.Connection, approval_request_id: str
) -> dict | None:
    """Fetch the candidate whose greenlight card is *approval_request_id*
    (``approval_requests.id``) — the tap-resolution lookup."""
    cursor = await db.execute(
        "SELECT * FROM build_candidates WHERE approval_request_id = ?",
        (approval_request_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_task(db: aiosqlite.Connection, task_id: str) -> dict | None:
    """Fetch the candidate tied to a submitted executor task
    (``task_states.task_id``) — the outcome-tracking lookup."""
    cursor = await db.execute(
        "SELECT * FROM build_candidates WHERE task_id = ?", (task_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_open(db: aiosqlite.Connection) -> list[dict]:
    """All undecided candidates, newest first."""
    cursor = await db.execute(
        "SELECT * FROM build_candidates "
        "WHERE user_decision IS NULL ORDER BY created_at DESC"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_recent(
    db: aiosqlite.Connection, *, limit: int = 50
) -> list[dict]:
    """Most recent candidates regardless of decision state, newest first."""
    cursor = await db.execute(
        "SELECT * FROM build_candidates ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_outcome(
    db: aiosqlite.Connection, outcome: str
) -> list[dict]:
    """All candidates in a given *outcome*, oldest first.

    Uses ``idx_build_candidates_outcome``. Unlike a recency-bounded scan,
    this never drops an in-flight candidate out of the window as unrelated
    calibration rows accumulate — the reconcile loop must see EVERY
    ``submitted`` row until its task reaches a terminal phase.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"invalid outcome: {outcome!r}")
    cursor = await db.execute(
        "SELECT * FROM build_candidates WHERE outcome = ? "
        "ORDER BY created_at ASC",
        (outcome,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_verdict(
    db: aiosqlite.Connection, verdict: str, *, limit: int = 50
) -> list[dict]:
    """All candidates with a given *verdict*, newest first.

    Used by the report's "wouldn't-build" section (verdict='dont_build') —
    ``list_by_outcome`` can't serve this because dont_build rows never leave
    ``outcome='pending'`` (they are closed via ``user_decision``, not outcome).
    """
    if verdict not in VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    cursor = await db.execute(
        "SELECT * FROM build_candidates WHERE verdict = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (verdict, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def verdict_decision_counts(db: aiosqlite.Connection) -> list[dict]:
    """Calibration aggregate: count of candidates grouped by (verdict,
    user_decision). ``user_decision`` is NULL for still-open candidates.

    Returns rows like ``{"verdict": "build", "user_decision": "approved",
    "count": 3}`` — the raw material for the report's per-verdict agreement
    lines (how often the user's decision matched Genesis's verdict).
    """
    cursor = await db.execute(
        "SELECT verdict, user_decision, COUNT(*) AS count "
        "FROM build_candidates "
        "GROUP BY verdict, user_decision "
        "ORDER BY verdict, user_decision"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def record_user_decision(
    db: aiosqlite.Connection,
    id: str,
    *,
    user_decision: str,
    decided_at: str | None = None,
) -> bool:
    """Close a candidate with the user's decision (calibration ground truth)."""
    if user_decision not in USER_DECISIONS:
        raise ValueError(f"invalid user_decision: {user_decision!r}")
    cursor = await db.execute(
        """UPDATE build_candidates
           SET user_decision = ?,
               decided_at = COALESCE(?, datetime('now')),
               updated_at = datetime('now')
           WHERE id = ?""",
        (user_decision, decided_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update(
    db: aiosqlite.Connection,
    id: str,
    *,
    plan_path: str | None = None,
    approval_request_id: str | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    pr_url: str | None = None,
    outcome: str | None = None,
    scope_gate_result: str | None = None,
) -> bool:
    """Update lifecycle fields (None = leave unchanged)."""
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"invalid outcome: {outcome!r}")
    updates = []
    params: list = []
    if plan_path is not None:
        updates.append("plan_path = ?")
        params.append(plan_path)
    if approval_request_id is not None:
        updates.append("approval_request_id = ?")
        params.append(approval_request_id)
    if task_id is not None:
        updates.append("task_id = ?")
        params.append(task_id)
    if branch is not None:
        updates.append("branch = ?")
        params.append(branch)
    if pr_url is not None:
        updates.append("pr_url = ?")
        params.append(pr_url)
    if outcome is not None:
        updates.append("outcome = ?")
        params.append(outcome)
    if scope_gate_result is not None:
        updates.append("scope_gate_result = ?")
        params.append(scope_gate_result)
    if not updates:
        return False
    updates.append("updated_at = datetime('now')")
    params.append(id)
    cursor = await db.execute(
        f"UPDATE build_candidates SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    await db.commit()
    return cursor.rowcount > 0
