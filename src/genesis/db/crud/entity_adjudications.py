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
    "policy",
)


#: Adjudication-policy version stamped on every verdict write. Bump when the
#: prompt POLICY changes meaning (not on wording tweaks): rows carrying an
#: older/absent stamp were judged under different rules, and settled_pair_keys
#: uses that to re-open pre-policy 'distinct' verdicts (MW-3 PR-2b — the
#: Option-1 same-referent policy superseded the sub-item-vs-parent rule that
#: had settled ~400 containment-class pairs as distinct).
POLICY_VERSION = "mw3-option1"


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
            created_at, applied_at, policy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
             applied_at = excluded.applied_at,
             -- policy MUST be stamped in the conflict-update too: a re-judged
             -- pre-policy row left at NULL would stay outside settled_pair_keys
             -- and be re-nominated every sweep — an infinite re-judge loop
             -- eating the whole drain budget (designed out at plan time,
             -- 2026-08-12 red-team; locked by
             -- test_rejudged_pre_policy_row_is_stamped).
             policy = excluded.policy,
             -- Approval INVALIDATION on a semantically-changed re-adjudication.
             -- Preserve the human approval ONLY when every field the apply path
             -- reads to execute the destructive merge is unchanged. If a re-judge
             -- flips loser_id/survivor_id (a DIRECTION swap the order-independent
             -- staleness set at entity_adjudication.py:606 does NOT catch — it
             -- compares {survivor_id, loser_id} as a set), or changes the entities /
             -- norm snapshots / verdict, the human approved a DIFFERENT merge than
             -- would now execute, so the old approval must not carry over — clear it
             -- and force re-review. A harmless re-judge that reaches the SAME
             -- decision (only provider/reasoning/updated_* changed) keeps approval.
             -- The field set here MUST equal _apply_one_proposal's staleness reads
             -- (entity_a/entity_b/norm_a/norm_b/survivor_id/loser_id) + verdict;
             -- excludes updated_a/updated_b (the apply path never reads them, so a
             -- routine re-judge that only refreshes them must not clear approval).
             -- Unqualified columns = the EXISTING row; excluded.* = the new values.
             -- `IS` is NULL-safe equality. Locked by
             -- test_reapprove_not_clobbered_by_readjudication (preserve) and
             -- test_readjudication_direction_flip_clears_approval (clear).
             approved_at = CASE
               WHEN entity_a IS excluded.entity_a
                AND entity_b IS excluded.entity_b
                AND loser_id IS excluded.loser_id
                AND survivor_id IS excluded.survivor_id
                AND norm_a IS excluded.norm_a
                AND norm_b IS excluded.norm_b
                AND verdict IS excluded.verdict
               THEN approved_at ELSE NULL END,
             approved_by = CASE
               WHEN entity_a IS excluded.entity_a
                AND entity_b IS excluded.entity_b
                AND loser_id IS excluded.loser_id
                AND survivor_id IS excluded.survivor_id
                AND norm_a IS excluded.norm_a
                AND norm_b IS excluded.norm_b
                AND verdict IS excluded.verdict
               THEN approved_by ELSE NULL END""",
        # approved_at / approved_by are absent from the INSERT column list (a fresh
        # proposal is never pre-approved) and CONDITIONALLY preserved on conflict by
        # the CASE above: a re-adjudication keeps approval iff the approved merge is
        # unchanged, else clears it (approval invalidation on semantic change).
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
            POLICY_VERSION,
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
    cursor = await db.execute(
        # Pre-policy 'distinct' re-opens (MW-3 PR-2b): a NULL policy stamp
        # means the verdict predates the Option-1 same-referent prompt, whose
        # predecessor settled real qualifier-variant pairs as distinct under a
        # sub-item-vs-parent rule. Excluding them lets the sweep re-nominate
        # exactly that class; merge/proposed_merge rows stay settled whatever
        # their stamp (re-running a merge decision buys nothing and risks
        # churn). Self-limiting: re-judged rows get stamped by record_verdict.
        "SELECT pair_key FROM entity_adjudications WHERE verdict != 'stale' "
        "AND NOT (verdict = 'distinct' AND policy IS NULL)"
    )
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
    # Clamp negative limit to 0. A negative SQLite LIMIT means UNLIMITED, so an
    # unclamped negative here would make the gated apply path (approved_only=True)
    # drain EVERY approved merge in one call regardless of the caller's budget —
    # the apply(budget=-1) unbounded-apply bug. 0 = apply nothing (safe).
    limit = max(int(limit), 0)
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

    Human-only: refuses a dispatched/unsupervised session at the CRUD layer (below the
    MCP wrapper) so a direct import can't self-approve. See ``guard_human_gate``.
    """
    from genesis.security.immunity_shadow import guard_human_gate

    guard_human_gate("entity_adjudication_approve")
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

    Human-only: refuses a dispatched/unsupervised session at the CRUD layer (reject
    carries the same human-review authority as approve). See ``guard_human_gate``.
    """
    from genesis.security.immunity_shadow import guard_human_gate

    guard_human_gate("entity_adjudication_reject")
    cursor = await db.execute(
        # policy is stamped here too: a human reject of a PRE-policy row left at
        # NULL would land in the re-open lane (settled_pair_keys excludes
        # NULL-policy distinct), so the sweep would re-judge — and possibly
        # re-propose — the very merge the human just declined, overwriting
        # their recorded reasoning. The re-open exists for OLD-PROMPT LLM
        # judgments; a human verdict is never a prompt-policy artifact.
        "UPDATE entity_adjudications SET verdict = 'distinct', policy = ?, "
        "approved_at = NULL, approved_by = NULL, reasoning = ? "
        "WHERE pair_key = ? AND verdict = 'proposed_merge'",
        (POLICY_VERSION, f"human-reject: {reason}", pair_key),
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
    # A negative SQLite LIMIT means UNLIMITED — clamp to 0 (return nothing) so a
    # negative limit can never silently dump the whole table.
    limit = max(int(limit), 0)
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


async def claim_approved_for_apply(
    db: aiosqlite.Connection,
    *,
    pair_key: str,
    loser_id: str,
    survivor_id: str,
    _commit: bool = True,
) -> bool:
    """Atomically CLAIM an approved proposed_merge for application.

    A single conditional UPDATE — verdict flips ``proposed_merge`` → ``merge``
    ONLY while it is still ``proposed_merge`` AND ``approved_at IS NOT NULL``.
    That predicate is the lock: SQLite serialises the write, so of two concurrent
    appliers (or an apply racing a reject) exactly one sees ``rowcount == 1`` and
    wins; the loser sees 0 and must NOT apply. This closes the read-then-apply
    TOCTOU where two processes both merged the same pair, or a reject landing
    after the read still applied the stale row. The caller runs the destructive
    ``merge_entity`` under the SAME savepoint so a failure rolls the claim back.

    ``loser_id``/``survivor_id`` are the STORED, human-approved direction — the
    caller passes the row's own values, never a freshly recomputed pick. They are
    ALSO part of the predicate, and that is load-bearing rather than defensive:
    this statement OVERWRITES both columns, so any field it writes without
    checking is a field a concurrent writer can lose.

    The window it closes (Codex round 5). The caller applies a batch snapshot
    taken before the transaction, and re-checks identity inside it — but that
    re-check compares ``{survivor_id, loser_id}`` as a SET, deliberately
    order-independent so it answers "same pair?" and NOT "same direction?".
    So a row that was marked stale, re-adjudicated with the direction FLIPPED,
    and re-approved by a human between the listing and its turn in the sequential
    batch passes staleness (same two entities, same norms) while the snapshot
    still holds the old direction. Without these two conjuncts the UPDATE would
    match, overwrite the freshly-approved direction with the stale one, and merge
    the WRONG entity — irreversibly, since ``merge_entity`` deletes the loser's
    mentions and links and there is no unmerge path.

    With them, a flipped row simply loses the claim (rowcount 0). The caller
    already treats that as ``skipped`` and the reconcile sweep re-picks it with
    the current direction, which is the correct outcome: a human approved THAT
    direction, not this one. Mirrors the field-by-field comparison
    ``record_verdict`` already uses to decide whether an approval survives.

    Returns True iff this caller won the claim.
    """
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "UPDATE entity_adjudications SET verdict = 'merge', loser_id = ?, "
        "survivor_id = ?, applied_at = ? "
        "WHERE pair_key = ? AND verdict = 'proposed_merge' "
        "AND approved_at IS NOT NULL "
        "AND loser_id IS ? AND survivor_id IS ?",
        (loser_id, survivor_id, now, pair_key, loser_id, survivor_id),
    )
    if _commit:
        await db.commit()
    return cursor.rowcount == 1


async def mark_stale(db: aiosqlite.Connection, *, pair_key: str, _commit: bool = True) -> bool:
    """Mark a still-``proposed_merge`` proposal that no longer holds (one side
    merged/renamed/gone). Returns True iff a row actually transitioned to stale.

    VOIDS any human approval. ``stale`` re-opens an approved row for re-proposal
    (the sweep excludes stale from settled_pair_keys, and record_verdict preserves
    approved_at across a re-adjudication that reaches the SAME decision). So if stale
    kept the approval, a merge approved under identity-state v1 could silently re-apply
    under drifted state v2 the human never saw — defeating the gate. Staleness means
    "the thing you approved changed": the approval must not survive.

    CONDITIONAL on ``verdict = 'proposed_merge'`` — the state the apply path operates
    on. Two concurrent appliers can list the same approved row; the winner flips
    proposed_merge→merge and commits, then the loser re-reads identities (now both
    resolving to the survivor), enters the staleness branch, and would otherwise
    unconditionally rewrite the applied ``merge`` back to ``stale`` — corrupting the
    ledger. The same race could overwrite a human ``distinct`` rejection. This stale
    write happens BEFORE ``claim_approved_for_apply``, so the atomic claim never
    arbitrates it; gating on ``proposed_merge`` makes it a no-op once the row has left
    that state (a concurrent winner merged, or a human rejected). Returns False then,
    and the caller counts the row as skipped rather than stale (Codex P2, #1477).
    """
    cursor = await db.execute(
        "UPDATE entity_adjudications SET verdict = 'stale', "
        "approved_at = NULL, approved_by = NULL "
        "WHERE pair_key = ? AND verdict = 'proposed_merge'",
        (pair_key,),
    )
    if _commit:
        await db.commit()
    return cursor.rowcount > 0
