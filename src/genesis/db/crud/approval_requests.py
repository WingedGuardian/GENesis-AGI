"""CRUD operations for approval_requests table."""

from __future__ import annotations

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
#           cleanup:* (housekeeping jobs)
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
    cursor = await db.execute(
        """UPDATE approval_requests
           SET context = ?
           WHERE id = ?""",
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
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           WHERE status = 'approved'
             AND consumed_at IS NULL
             AND json_extract(context, '$.subsystem') = ?
             AND json_extract(context, '$.policy_id') = ?
             AND resolved_at > datetime('now', '-24 hours')
           ORDER BY resolved_at DESC
           LIMIT 1""",
        (subsystem, policy_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM approval_requests WHERE id = ?", (id,)
    )
    await db.commit()
    return cursor.rowcount > 0
