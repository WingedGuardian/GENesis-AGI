"""CRUD operations for approval_requests table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

# ── Resolver-origin classification ───────────────────────────────────────────
# ``resolved_by`` is free text written by convention, not constraint. This is
# the ONE canonical mapping from resolved_by values to a resolver class —
# consumers (J-9 approvals metrics, dashboards) must use it rather than
# re-deriving prefixes. When adding a new resolved_by writer, extend the
# prefix tuples AND tests/test_db/test_approval_resolver_classes.py (which
# pins every known writer literal and the live-DB value inventory).
#
# Known writers as of 2026-07-09:
#   human:  telegram:batch:/button:/bare_text: (channels/telegram/
#           _handler_messages.py), <channel>:reply (autonomy/approval_gate.py),
#           dashboard / dashboard:batch (dashboard/routes/state.py),
#           voice:s2s (channels/voice/genesis_bridge.py),
#           manual:* (operator sessions), "user" (autonomy/approval.py default)
#   system: "system" (approval.py cancel()), timeout_auto_expire
#           (approval_gate.py), alarm_cleared (sentinel/dispatcher.py),
#           cleanup:* (housekeeping jobs), genesis:* (autonomous Genesis-vetted
#           self-approvals, e.g. genesis:contributor-worklog in
#           mcp/health/contributor_issue.py when require_approval is off)
#   blank/None → system: bulk expiry (expire_timed_out below) never writes
#           resolved_by — no human acted on those rows.
HUMAN_RESOLVER_PREFIXES: tuple[str, ...] = (
    "telegram:",
    "dashboard",
    "voice:",
    "manual:",
    "user",
)
SYSTEM_RESOLVER_PREFIXES: tuple[str, ...] = (
    "system",
    "timeout_auto_expire",
    "cleanup:",
    "alarm_cleared",
    "genesis:",  # autonomous Genesis-vetted self-approval (NOT human-earned authority)
)


def classify_resolver(resolved_by: str | None) -> str:
    """Classify a ``resolved_by`` value as ``human`` | ``system`` | ``unknown``.

    Unmatched non-blank values return ``unknown`` — never guessed: the live DB
    carries values with no in-tree writer (one-off manual DB fixes such as
    ``manual_stale_cleanup``), and misclassifying a novel human channel as
    system would silently deflate the human-resolution metrics. Consumers
    surface unknowns rather than bucketing them.
    """
    if resolved_by is None or not resolved_by.strip():
        return "system"
    value = resolved_by.strip()
    if value.startswith(HUMAN_RESOLVER_PREFIXES):
        return "human"
    if value.startswith(SYSTEM_RESOLVER_PREFIXES):
        return "system"
    return "unknown"


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    action_type: str,
    action_class: str,
    description: str,
    context: str | None = None,
    status: str = "pending",
    timeout_at: str | None = None,
    created_at: str | None = None,
    content_hash: str | None = None,
    previous_hash: str | None = None,
    chain_hash: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO approval_requests
           (id, action_type, action_class, description, context,
            status, timeout_at, created_at,
            content_hash, previous_hash, chain_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')),
                   ?, ?, ?)""",
        (id, action_type, action_class, description, context,
         status, timeout_at, created_at,
         content_hash, previous_hash, chain_hash),
    )
    await db.commit()
    return id


async def create_chained(
    db: aiosqlite.Connection,
    *,
    id: str,
    action_type: str,
    action_class: str,
    description: str,
    context: str | None = None,
    status: str = "pending",
    timeout_at: str | None = None,
    created_at: str | None = None,
) -> str:
    """Insert approval request with hash chain.

    Note: the read-then-insert has a theoretical TOCTOU race if two
    requests are created in the same asyncio tick. This is low-probability
    and detectable: two records sharing the same previous_hash indicates
    a fork (concurrent write), not tampering. verify_chain() catches it.
    """
    from genesis.ego.integrity import canonical_json, chained_hash, content_hash

    c_hash = content_hash(canonical_json({
        "action_type": action_type,
        "action_class": action_class,
        "description": description,
        "context": context or "",
    }))

    cursor = await db.execute(
        "SELECT chain_hash FROM approval_requests "
        "WHERE chain_hash IS NOT NULL "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    prev_chain = row[0] if row else None

    chain = chained_hash(c_hash, prev_chain)

    await db.execute(
        """INSERT INTO approval_requests
           (id, action_type, action_class, description, context,
            status, timeout_at, created_at,
            content_hash, previous_hash, chain_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')),
                   ?, ?, ?)""",
        (id, action_type, action_class, description, context,
         status, timeout_at, created_at,
         c_hash, prev_chain, chain),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM approval_requests WHERE id = ?", (id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_pending(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           WHERE status = 'pending'
           ORDER BY created_at ASC""",
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_recent(
    db: aiosqlite.Connection, *, limit: int = 200,
) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_expired(
    db: aiosqlite.Connection, *, now: str
) -> list[dict]:
    """Find pending requests whose timeout has passed."""
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           WHERE status = 'pending'
             AND timeout_at IS NOT NULL
             AND timeout_at <= ?
           ORDER BY timeout_at ASC""",
        (now,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def resolve(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
    resolved_at: str,
    resolved_by: str | None = None,
) -> bool:
    """Resolve a request (approve, reject, expire, cancel)."""
    cursor = await db.execute(
        """UPDATE approval_requests
           SET status = ?, resolved_at = ?, resolved_by = ?
           WHERE id = ?
             AND status = 'pending'""",
        (status, resolved_at, resolved_by, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_context(
    db: aiosqlite.Connection, id: str, *, context: str,
) -> bool:
    """Rewrite a PENDING request's context.

    Restricted to pending rows on purpose: ``context`` is no longer only
    display data — the desktop gate's authorization predicate reads
    ``$.kind`` and ``$.session_id`` out of it, so an unrestricted update would
    let a resolved grant's scope be rewritten after the owner approved it.
    The sole caller (``approval_gate``, on its own request ids) only ever
    updates pending rows, so this narrows nothing that was in use.
    """
    cursor = await db.execute(
        """UPDATE approval_requests
           SET context = ?
           WHERE id = ?
             AND status = 'pending'""",
        (context, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def expire_timed_out(
    db: aiosqlite.Connection, *, now: str
) -> int:
    """Bulk-expire all pending requests past their timeout. Returns count."""
    cursor = await db.execute(
        """UPDATE approval_requests
           SET status = 'expired', resolved_at = ?
           WHERE status = 'pending'
             AND timeout_at IS NOT NULL
             AND timeout_at <= ?""",
        (now, now),
    )
    await db.commit()
    return cursor.rowcount


async def mark_consumed(
    db: aiosqlite.Connection, id: str, *, consumed_at: str,
) -> bool:
    """Mark an approved request as consumed (action was dispatched).

    Atomic: only updates if consumed_at IS NULL, preventing double-dispatch.
    Returns True if this call consumed it, False if already consumed.
    """
    cursor = await db.execute(
        """UPDATE approval_requests
           SET consumed_at = ?
           WHERE id = ?
             AND status = 'approved'
             AND consumed_at IS NULL""",
        (consumed_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def find_approved_unconsumed(
    db: aiosqlite.Connection,
    *,
    subsystem: str,
    policy_id: str,
) -> dict | None:
    """Find an approved request that hasn't been consumed yet.

    Used by the resume mechanism: when an approval is granted (via Telegram
    or dashboard), the blocked action can resume on the next tick.
    """
    # The cutoff is computed in PYTHON, not as datetime('now','-24 hours').
    # `resolved_at` is written by `ApprovalManager.resolve` as
    # `datetime.now(UTC).isoformat()` -> "2026-09-08T05:22:44.814096+00:00",
    # while SQLite renders its own threshold as "2026-09-08 21:22:44". The
    # comparison is lexicographic, and 'T' (0x54) > ' ' (0x20), so ANY row
    # sharing the threshold's DATE compared greater regardless of its time —
    # MEASURED: a 40-hour-old approval passed a window documented as 24 hours
    # (a 70-hour-old one did not, so the window stretched to ~48h rather than
    # opening entirely). Fail-open on a staleness guard. Binding a Python-side
    # ISO cutoff puts both sides in the writer's format; the same fix is
    # recorded at observations.py:733, and approval_gate.py:616-624 already
    # does this comparison correctly in Python.
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           WHERE status = 'approved'
             AND consumed_at IS NULL
             AND (CASE WHEN json_valid(context)
                       THEN json_extract(context, '$.subsystem') END) = ?
             AND (CASE WHEN json_valid(context)
                       THEN json_extract(context, '$.policy_id') END) = ?
             AND resolved_at > ?
           ORDER BY resolved_at DESC
           LIMIT 1""",
        (subsystem, policy_id, cutoff),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def claim_approved_for_task(
    db: aiosqlite.Connection, *, task_id: str, action_type: str,
) -> str | None:
    """Atomically CLAIM an approved, unconsumed resume approval for *task_id*.

    Returns the claimed request id, or ``None`` when there is nothing to claim.

    Claiming CONSUMES the row. Lookup and consumption are one operation on
    purpose: the two resume call sites (``dispatcher.dispatch_cycle`` Path 1b
    and ``dispatcher.recover_incomplete``) previously looked up a row and
    dispatched WITHOUT consuming it, so the approval stayed approved-and-
    unconsumed forever and a SECOND block on the same task resumed on the first
    block's human decision. Exposing a find-only helper here would leave that
    ordering up to each caller again; there is no such helper by design.

    Matching is SEMANTIC (``json_extract``), not textual. The callers used
    ``context LIKE '%"task_id": "<id>"%'``, which encodes ``json.dumps``'
    default separators — a producer serialising compactly writes
    ``{"task_id":"t1"}`` and the scan silently matched nothing, stranding the
    task exactly as if no approval had been granted. ``request_approval``
    takes ``context`` as an opaque ``str``, so both spellings are legal. The
    ``json_valid`` guard mirrors :func:`find_approved_unconsumed`; a
    non-JSON context is simply not a match rather than a SQL error.

    *action_type* is REQUIRED and is matched too, so the id alone never
    authorises a release. ``$.task_id`` is not a free namespace: an
    autonomous-CLI-fallback approval for a task STEP carries the same task id
    at ``$.extra.task_id`` (``executor/step_dispatcher.py`` builds it,
    ``approval_gate`` nests it), and that is the id of the very task most
    likely to block next. MEASURED: the textual scan this replaces DID match
    that nested context, so an unconsumed CLI-fallback approval could release
    a blocked task it was never granted for. Today the nesting alone would
    hide it; requiring the type means correctness no longer rests on an
    undocumented decision in another module.

    Deliberately NO ``resolved_at`` window, unlike :func:`find_approved_unconsumed`.
    That function's 24h window suits a short-lived autonomous-CLI grant; a
    blocked task waits on a human who may answer days later, and expiring the
    claim would re-strand the very task this exists to free.
    """
    _TASK_MATCH = """(CASE WHEN json_valid(context)
                            THEN json_extract(context, '$.task_id') END) = ?"""

    # Step 1: the NEWEST HUMAN ANSWER for this task, whatever it said.
    # Filtering to status='approved' FIRST cannot enforce "the newest answer
    # governs", because a later rejection or cancellation is excluded by the
    # very filter that is supposed to be ranked. The user approving an older
    # duplicate and then rejecting a newer one would leave the approval live
    # and the rejection invisible. Ranking over ALL resolved rows is what
    # makes the docstring true. Unresolved (pending) rows carry a NULL
    # resolved_at and are excluded here -- an unanswered card is not an
    # answer -- but they are retired in step 3.
    cursor = await db.execute(
        f"""SELECT id, status FROM approval_requests
             WHERE consumed_at IS NULL
               AND resolved_at IS NOT NULL
               AND action_type = ?
               AND {_TASK_MATCH}
             ORDER BY resolved_at DESC
             LIMIT 1""",
        (action_type, task_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    request_id = row["id"] if isinstance(row, aiosqlite.Row) else row[0]
    status = row["status"] if isinstance(row, aiosqlite.Row) else row[1]
    if status != "approved":
        # The newest answer was negative. Nothing is claimed and nothing is
        # retired: a rejection is not a licence to invalidate other rows.
        return None

    now = datetime.now(UTC).isoformat()

    # Step 2: claim it atomically. The consumed_at IS NULL predicate plus the
    # rowcount check is what makes a concurrent claimer lose rather than
    # produce a second dispatch of the same task. Inlined rather than calling
    # mark_consumed because that helper COMMITS, which would split this claim
    # into two transactions -- and a failure in between would leave the
    # approval permanently spent with its siblings still live, i.e. the exact
    # state this function exists to prevent.
    cursor = await db.execute(
        """UPDATE approval_requests SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL""",
        (now, request_id),
    )
    if cursor.rowcount == 0:
        await db.rollback()
        return None

    # Step 3: retire EVERY remaining sibling for this task, in the SAME
    # transaction and regardless of status. Approved siblings would otherwise
    # buy N free resumes; a PENDING sibling is worse, because it stays
    # answerable -- the user taps it later, after the task has reached a
    # DIFFERENT blocker, and that stale card releases a block nobody approved.
    # The invariant: ONE human answer releases ONE block, and an answer to an
    # earlier block never releases a later one.
    await db.execute(
        f"""UPDATE approval_requests
               SET consumed_at = ?
             WHERE consumed_at IS NULL
               AND action_type = ?
               AND {_TASK_MATCH}""",
        (now, action_type, task_id),
    )
    await db.commit()
    return request_id


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM approval_requests WHERE id = ?", (id,)
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_approved_unconsumed_for_session(
    db: aiosqlite.Connection,
    *,
    action_type: str,
    session_id: str,
    kind: str,
) -> list[dict]:
    """Approved, unconsumed rows of *action_type* + *kind* for *session_id*.

    The desktop-takeover gate's session-consent lookup. Returns every match,
    newest resolution first, rather than the single newest row: the caller must
    additionally require an allowlisted resolver and an unexpired grant, and a
    ``LIMIT 1`` here would let one system-resolved row hide an older, valid
    owner approval underneath it.

    ``kind`` is NOT optional and NOT cosmetic. One action_type carries two
    different kinds of row — a session GRANT and a per-action HOLD — because a
    single action_type is what keeps every batch-approval exclusion to one
    entry. Without this predicate the two are indistinguishable, and approving
    one held action silently becomes a full session grant with a fresh TTL:
    the owner consents to one click and hands over the session. MEASURED
    before the fix; regression-tested after.

    The ``json_extract`` calls are guarded by ``json_valid`` inside a CASE,
    which SQLite evaluates lazily. A bare ``json_extract`` raises "malformed
    JSON" on an invalid value, and a bare AND-chain does not promise to
    short-circuit before reaching it — so ONE hand-edited or corrupted
    ``context`` anywhere in the table (the module docstring above names
    one-off manual DB fixes as a real occurrence) made every desktop grant
    lookup raise. MEASURED: an unguarded query against two malformed rows
    raised OperationalError. It failed closed, but a gate that crashes is a
    gate nobody can use.

    Deliberately unbounded: the result is scoped to one action_type AND one
    session id, and a session holds one grant by construction (a fresh row per
    session, consumed at teardown), so the population is a handful of rows.

    Unlike :func:`find_approved_unconsumed` there is no ``resolved_at`` window
    in SQL — the grant's lifetime is a config lever the caller owns, and
    encoding a second, silently different expiry here would give the capability
    two disagreeing clocks.
    """
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           WHERE status = 'approved'
             AND consumed_at IS NULL
             AND action_type = ?
             AND (CASE WHEN json_valid(context)
                       THEN json_extract(context, '$.kind') END) = ?
             AND (CASE WHEN json_valid(context)
                       THEN json_extract(context, '$.session_id') END) = ?
           ORDER BY resolved_at DESC""",
        (action_type, kind, session_id),
    )
    return [dict(r) for r in await cursor.fetchall()]
