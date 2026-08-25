"""CRUD for entity_adjudications — the entity-node merge-vs-distinct ledger.

One row per fuzzy entity PAIR (order-independent ``pair_key``). See the table
docstring in ``db/schema/_tables.py`` and migration 0065 for the column model.
This is NOT ``entity_resolution_audit`` (memory-pair dedup) — it records whether
two ENTITY NODES are the same real-world thing.

Reads build dicts from an explicit column list rather than relying on
``row_factory`` (the shared connection's factory is not guaranteed).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import aiosqlite

_COLS = (
    "id",
    "pair_key",
    "entity_a",
    "entity_b",
    "loser_id",
    "survivor_id",
    "verdict",
    "reasoning",
    "provider",
    "mode",
    "norm_a",
    "norm_b",
    "updated_a",
    "updated_b",
    "created_at",
    "applied_at",
    "approved_at",
    "approved_by",
)


def pair_key(entity_a: str, entity_b: str) -> str:
    """Order-independent dedup key: sorted id pair joined by '|'."""
    lo, hi = sorted((entity_a, entity_b))
    return f"{lo}|{hi}"


def _row_to_dict(row: tuple) -> dict:
    return dict(zip(_COLS, row, strict=True))


async def record_verdict(
    db: aiosqlite.Connection,
    *,
    entity_a: str,
    entity_b: str,
    verdict: str,
    reasoning: str | None = None,
    provider: str | None = None,
    mode: str | None = None,
    loser_id: str | None = None,
    survivor_id: str | None = None,
    norm_a: str | None = None,
    norm_b: str | None = None,
    updated_a: str | None = None,
    updated_b: str | None = None,
    applied_at: str | None = None,
    _commit: bool = True,
) -> str:
    """Upsert a verdict keyed on the order-independent pair.

    Overwrite-on-conflict (latest judgment wins): a re-adjudicated ``stale``
    pair records its fresh verdict rather than being silently ignored. Callers
    that must not re-judge an already-decided pair check ``get_by_pair`` first.
    Returns the row id (fresh uuid on insert; existing id on conflict-update).
    """
    key = pair_key(entity_a, entity_b)
    now = datetime.now(UTC).isoformat()
    row_id = uuid.uuid4().hex[:16]
    cursor = await db.execute(
        """INSERT INTO entity_adjudications
           (id, pair_key, entity_a, entity_b, loser_id, survivor_id, verdict,
            reasoning, provider, mode, norm_a, norm_b, updated_a, updated_b,
            created_at, applied_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(pair_key) DO UPDATE SET
             entity_a = excluded.entity_a,
             entity_b = excluded.entity_b,
             loser_id = excluded.loser_id,
             survivor_id = excluded.survivor_id,
             verdict = excluded.verdict,
             reasoning = excluded.reasoning,
             provider = excluded.provider,
             mode = excluded.mode,
             norm_a = excluded.norm_a,
             norm_b = excluded.norm_b,
             updated_a = excluded.updated_a,
             updated_b = excluded.updated_b,
             applied_at = excluded.applied_at""",
        # DELIBERATELY absent from the INSERT column list and the ON CONFLICT SET:
        # approved_at / approved_by. A re-adjudication of an already-approved pair
        # (e.g. the strong-model re-judge) must NEVER wipe a human approval — the
        # gate depends on it. Locked by test_reapprove_not_clobbered_by_readjudication.
        (
            row_id,
            key,
            entity_a,
            entity_b,
            loser_id,
            survivor_id,
            verdict,
            reasoning,
            provider,
            mode,
            norm_a,
            norm_b,
            updated_a,
            updated_b,
            now,
            applied_at,
        ),
    )
    if _commit:
        await db.commit()
    # On conflict the stored id is the pre-existing one; return whatever the row holds.
    if cursor.rowcount == 0:  # pragma: no cover — defensive; upsert always writes
        existing = await get_by_pair(db, entity_a, entity_b)
        return existing["id"] if existing else row_id
    row = await get_by_pair(db, entity_a, entity_b)
    return row["id"] if row else row_id


async def get_by_pair(db: aiosqlite.Connection, entity_a: str, entity_b: str) -> dict | None:
    """Fetch the verdict row for a pair (order-independent), or None."""
    key = pair_key(entity_a, entity_b)
    cursor = await db.execute(
        f"SELECT {', '.join(_COLS)} FROM entity_adjudications WHERE pair_key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def all_pair_keys(db: aiosqlite.Connection) -> set[str]:
    """Every recorded pair_key (any verdict). Bounded by total fuzzy pairs
    (low thousands)."""
    cursor = await db.execute("SELECT pair_key FROM entity_adjudications")
    return {r[0] for r in await cursor.fetchall()}


async def settled_pair_keys(db: aiosqlite.Connection) -> set[str]:
    """pair_keys with a SETTLED verdict (merge/distinct/proposed_merge) — the
    sweep's dedup set. Deliberately EXCLUDES ``stale``: a stale verdict means the
    prior judgment no longer holds (identity drifted), so the sweep SHOULD
    rediscover the pair and the drainer re-adjudicate it — otherwise a stale pair
    would be a permanent dead end."""
    cursor = await db.execute("SELECT pair_key FROM entity_adjudications WHERE verdict != 'stale'")
    return {r[0] for r in await cursor.fetchall()}


async def list_proposed_merges(
    db: aiosqlite.Connection, *, limit: int = 100, approved_only: bool = False
) -> list[dict]:
    """proposed_merge rows, oldest first.

    ``approved_only=True`` (the apply path) restricts to rows a human has approved
    (``approved_at IS NOT NULL``) — the gate that prevents any un-reviewed merge
    from being applied, even after the drainer flips to ``live``. ``False`` (the
    review surface) lists ALL proposals so a human can triage them.
    """
    clause = "verdict = 'proposed_merge'"
    if approved_only:
        clause += " AND approved_at IS NOT NULL"
    cursor = await db.execute(
        f"SELECT {', '.join(_COLS)} FROM entity_adjudications "
        f"WHERE {clause} ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    return [_row_to_dict(r) for r in await cursor.fetchall()]


async def approve(
    db: aiosqlite.Connection, *, pair_key: str, approved_by: str, _commit: bool = True
) -> bool:
    """Human-approve a proposed_merge for application. Returns True if a row moved.

    Only a ``proposed_merge`` row can be approved (an already-applied 'merge' or a
    'distinct'/'stale' verdict is left untouched). Idempotent: re-approving an
    already-approved row is a no-op that still returns True.
    """
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE entity_adjudications SET approved_at = COALESCE(approved_at, ?), "
        "approved_by = COALESCE(approved_by, ?) "
        "WHERE pair_key = ? AND verdict = 'proposed_merge'",
        (now, approved_by, pair_key),
    )
    if _commit:
        await db.commit()
    return cursor.rowcount > 0


async def reject(
    db: aiosqlite.Connection, *, pair_key: str, reason: str, _commit: bool = True
) -> bool:
    """Human-reject a proposed_merge: record it as 'distinct' so it is never applied.

    A 'distinct' verdict lands in ``settled_pair_keys`` (excluded from the sweep's
    re-nomination), so the pair does not bounce back. Returns True if a row moved.
    Clears any prior approval on the row (the human's final call is 'do not merge').
    """
    cursor = await db.execute(
        "UPDATE entity_adjudications SET verdict = 'distinct', approved_at = NULL, "
        "approved_by = NULL, reasoning = ? WHERE pair_key = ? AND verdict = 'proposed_merge'",
        (f"human-reject: {reason}", pair_key),
    )
    if _commit:
        await db.commit()
    return cursor.rowcount > 0


async def list_for_review(
    db: aiosqlite.Connection, *, status: str = "proposed", limit: int = 100
) -> list[dict]:
    """List adjudication rows for the human review surface.

    ``status``: 'proposed' (unapproved proposals awaiting review), 'approved'
    (approved, awaiting apply), or 'all' proposed_merge rows regardless of approval.
    """
    if status == "proposed":
        clause = "verdict = 'proposed_merge' AND approved_at IS NULL"
    elif status == "approved":
        clause = "verdict = 'proposed_merge' AND approved_at IS NOT NULL"
    elif status == "all":
        clause = "verdict = 'proposed_merge'"
    else:
        raise ValueError(f"list_for_review: unknown status {status!r}")
    cursor = await db.execute(
        f"SELECT {', '.join(_COLS)} FROM entity_adjudications "
        f"WHERE {clause} ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    return [_row_to_dict(r) for r in await cursor.fetchall()]


async def mark_applied(
    db: aiosqlite.Connection,
    *,
    pair_key: str,
    loser_id: str,
    survivor_id: str,
    _commit: bool = True,
) -> None:
    """Promote a proposed_merge to an applied merge (verdict='merge')."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE entity_adjudications SET verdict = 'merge', loser_id = ?, "
        "survivor_id = ?, applied_at = ? WHERE pair_key = ?",
        (loser_id, survivor_id, now, pair_key),
    )
    if _commit:
        await db.commit()


async def mark_stale(db: aiosqlite.Connection, *, pair_key: str, _commit: bool = True) -> None:
    """Mark a proposal that no longer holds (one side merged/renamed/gone)."""
    await db.execute(
        "UPDATE entity_adjudications SET verdict = 'stale' WHERE pair_key = ?",
        (pair_key,),
    )
    if _commit:
        await db.commit()
