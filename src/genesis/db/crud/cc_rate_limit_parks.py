"""CRUD for cc_rate_limit_parks — durable rate-limit park + resume lineage.

A park row is the durable lineage object for one logical unit of work that hit a
CC rate limit. It survives restarts (DB-backed) and is resolved by ``id`` across
an async re-dispatch cycle: the resume engine claims ``parked→resuming`` and
re-runs the work; the retry's outcome resolves THIS row — ``resumed`` on success,
or ``relimit`` (attempts+1, fresh reset, backoff) on a re-limit. Marking a park
terminal at dispatch would sever that lineage (attempts never accumulate, the
``needs_user`` escalation becomes unreachable) — so the row stays ``resuming``
between dispatch and outcome, reclaimed by ``recover_stale_resuming`` if the
process dies mid-flight.

Mirrors ``direct_session_queue`` (single-statement claim via UPDATE…RETURNING).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

# Open states hold the partial-unique dedup index — one open park per dedup_key.
_OPEN = ("parked", "resuming")


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def upsert_open_park(
    db: aiosqlite.Connection,
    *,
    kind: str,
    dedup_key: str,
    payload: dict,
    origin_session_id: str | None,
    limit_kind: str,
    raw_signal: str | None,
    reset_at: str | None,
    next_attempt_at: str,
) -> str:
    """Insert a fresh park, or bump the existing OPEN park with the same
    dedup_key (idempotent concurrent parks). Returns the park id.

    Used for FRESH parks (foreground turn, first background rate-limit) that
    carry no lineage. A re-limit of an already-resumed job is resolved by id via
    ``relimit`` instead — never through this path.
    """
    park_id = f"rlp-{uuid.uuid4().hex[:12]}"
    now = _now()
    cursor = await db.execute(
        """INSERT INTO cc_rate_limit_parks
               (id, kind, dedup_key, payload_json, origin_session_id, limit_kind,
                raw_signal, reset_at, status, attempts, next_attempt_at,
                created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'parked', 0, ?, ?, ?)
           ON CONFLICT(dedup_key) WHERE status IN ('parked', 'resuming')
           DO UPDATE SET
               attempts        = attempts + 1,
               reset_at        = excluded.reset_at,
               next_attempt_at = excluded.next_attempt_at,
               raw_signal      = excluded.raw_signal,
               limit_kind      = excluded.limit_kind,
               updated_at      = excluded.updated_at
           RETURNING id""",
        (
            park_id,
            kind,
            dedup_key,
            json.dumps(payload),
            origin_session_id,
            limit_kind,
            raw_signal,
            reset_at,
            next_attempt_at,
            now,
            now,
        ),
    )
    row = await cursor.fetchone()
    await db.commit()
    return row[0] if row else park_id


async def list_due(
    db: aiosqlite.Connection,
    *,
    now: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Open parks whose next_attempt_at is due (<= now). Oldest first."""
    now = now or _now()
    cursor = await db.execute(
        """SELECT * FROM cc_rate_limit_parks
           WHERE status = 'parked'
             AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
           ORDER BY created_at
           LIMIT ?""",
        (now, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def claim(db: aiosqlite.Connection, park_id: str) -> bool:
    """Atomically claim a due park (parked→resuming). True iff this caller won."""
    now = _now()
    cursor = await db.execute(
        """UPDATE cc_rate_limit_parks
           SET status = 'resuming', claimed_at = ?, updated_at = ?
           WHERE id = ? AND status = 'parked'""",
        (now, now, park_id),
    )
    await db.commit()
    return cursor.rowcount == 1


async def mark_resumed(db: aiosqlite.Connection, park_id: str) -> bool:
    """Terminal success: resuming→resumed. Called from the retry's completion
    hook AFTER the result is delivered. True iff the row was resuming."""
    now = _now()
    cursor = await db.execute(
        """UPDATE cc_rate_limit_parks
           SET status = 'resumed', claimed_at = NULL, updated_at = ?
           WHERE id = ? AND status = 'resuming'""",
        (now, park_id),
    )
    await db.commit()
    return cursor.rowcount == 1


async def relimit(
    db: aiosqlite.Connection,
    park_id: str,
    *,
    reset_at: str | None,
    next_attempt_at: str,
    needs_user_at_attempts: int,
) -> str:
    """A resumed retry hit the limit again — update THIS row in place (attempts+1,
    fresh reset, backoff). Escalates to ``needs_user`` once attempts reaches the
    threshold. Returns the resulting status ('parked' | 'needs_user' | '' if the
    row was not resolvable). Increments regardless of the prior status so a
    re-limit is never lost, but only re-opens (→parked) from an in-flight row.
    """
    now = _now()
    cursor = await db.execute(
        """UPDATE cc_rate_limit_parks
           SET attempts        = attempts + 1,
               reset_at        = ?,
               next_attempt_at = ?,
               claimed_at      = NULL,
               status          = CASE
                   WHEN attempts + 1 >= ? THEN 'needs_user'
                   ELSE 'parked'
               END,
               updated_at      = ?
           WHERE id = ? AND status IN ('resuming', 'parked')
           RETURNING status""",
        (reset_at, next_attempt_at, needs_user_at_attempts, now, park_id),
    )
    row = await cursor.fetchone()
    await db.commit()
    return row[0] if row else ""


async def recover_stale_resuming(
    db: aiosqlite.Connection,
    *,
    max_age_s: int = 7200,
) -> int:
    """Reclaim parks stuck in 'resuming' longer than max_age_s (retry lost to a
    crash/restart between claim and outcome) back to 'parked' for another try.

    max_age_s defaults to 2h — comfortably longer than a direct_session timeout
    (1h) so an in-flight resume is never yanked out from under a live retry.
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_s)).isoformat()
    now = _now()
    cursor = await db.execute(
        """UPDATE cc_rate_limit_parks
           SET status = 'parked', claimed_at = NULL, updated_at = ?
           WHERE status = 'resuming'
             AND (claimed_at IS NULL OR claimed_at < ?)""",
        (now, cutoff),
    )
    await db.commit()
    return cursor.rowcount


async def mark_terminal(db: aiosqlite.Connection, park_id: str, status: str) -> None:
    """Force a terminal status (cancelled/expired) — e.g. a cancel-path absorb."""
    now = _now()
    await db.execute(
        """UPDATE cc_rate_limit_parks
           SET status = ?, claimed_at = NULL, updated_at = ?
           WHERE id = ?""",
        (status, now, park_id),
    )
    await db.commit()


async def get_by_id(db: aiosqlite.Connection, park_id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM cc_rate_limit_parks WHERE id = ?",
        (park_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def count_open(db: aiosqlite.Connection) -> int:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM cc_rate_limit_parks WHERE status IN ('parked', 'resuming')",
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def prune_terminal(db: aiosqlite.Connection, *, older_than_days: int = 45) -> int:
    """Delete finished parks (resumed/cancelled/expired) past the retention
    window. ``needs_user`` is NEVER pruned — it awaits the user."""
    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
    cursor = await db.execute(
        """DELETE FROM cc_rate_limit_parks
           WHERE status IN ('resumed', 'cancelled', 'expired')
             AND updated_at < ?""",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount
