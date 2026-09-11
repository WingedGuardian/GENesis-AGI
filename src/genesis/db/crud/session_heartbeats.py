"""CRUD operations for session_heartbeats table.

Provides both async (runtime) and sync (hook) versions for cross-session
awareness. The proactive memory hook uses sync versions for speed (<5ms);
the runtime uses async versions for cleanup and queries.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)

# Sessions not updated within this window are considered stale
_STALE_THRESHOLD = timedelta(minutes=10)

# Why the conflict clause COALESCEs almost everything (both upserts below): this
# row has SEVERAL independent PARTIAL writers, each knowing a different subset --
# the UserPromptSubmit hook knows the prompt and the tool digest, a tool-use
# refresh knows only that the session is still alive, and `model` comes from a
# cache a DIFFERENT hook fills at SessionStart. A writer that omits a column is
# saying "I do not know this", never "clear it", and the model cache is bounded
# at 24 entries with insertion-order eviction -- so an evicted long-lived session
# would otherwise DESTROY its stored model on its next write. `source_tag` is the
# deliberate exception: it has a NOT NULL default, so omitting it is meaningful.
# Pinned by tests/test_db/test_session_heartbeats_upsert.py.


# ---------------------------------------------------------------------------
# Async versions (for runtime use)
# ---------------------------------------------------------------------------


async def upsert(
    db: aiosqlite.Connection,
    *,
    cc_session_id: str,
    source_tag: str = "foreground",
    model: str | None = None,
    topic: str | None = None,
    user_summary: str | None = None,
    genesis_summary: str | None = None,
) -> None:
    """Write or update a session heartbeat."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO session_heartbeats
           (cc_session_id, source_tag, model, topic, user_summary,
            genesis_summary, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(cc_session_id) DO UPDATE SET
             source_tag = excluded.source_tag,
             model = COALESCE(excluded.model, session_heartbeats.model),
             topic = COALESCE(excluded.topic, session_heartbeats.topic),
             user_summary = COALESCE(excluded.user_summary,
                                     session_heartbeats.user_summary),
             genesis_summary = COALESCE(excluded.genesis_summary,
                                        session_heartbeats.genesis_summary),
             updated_at = excluded.updated_at""",
        (cc_session_id, source_tag, model, topic, user_summary,
         genesis_summary, now),
    )
    await db.commit()


async def get_active(
    db: aiosqlite.Connection,
    *,
    exclude_session: str | None = None,
) -> list[dict]:
    """Get active heartbeats (updated within _STALE_THRESHOLD), excluding self."""
    cutoff = (datetime.now(UTC) - _STALE_THRESHOLD).isoformat()
    sql = (
        "SELECT cc_session_id, source_tag, model, topic, "
        "user_summary, genesis_summary, updated_at "
        "FROM session_heartbeats WHERE updated_at > ?"
    )
    params: list = [cutoff]
    if exclude_session:
        sql += " AND cc_session_id != ?"
        params.append(exclude_session)
    sql += " ORDER BY updated_at DESC"

    cursor = await db.execute(sql, params)
    return [dict(row) for row in await cursor.fetchall()]


async def cleanup_stale(db: aiosqlite.Connection) -> int:
    """Delete heartbeats older than _STALE_THRESHOLD. Returns count deleted."""
    cutoff = (datetime.now(UTC) - _STALE_THRESHOLD).isoformat()
    cursor = await db.execute(
        "DELETE FROM session_heartbeats WHERE updated_at < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Sync versions (for hook use — must be fast, no async overhead)
# ---------------------------------------------------------------------------


def upsert_sync(
    db_path: str,
    *,
    cc_session_id: str,
    source_tag: str = "foreground",
    model: str | None = None,
    topic: str | None = None,
    user_summary: str | None = None,
    genesis_summary: str | None = None,
    timeout: float = 1.0,
) -> None:
    """Sync heartbeat write for hooks. Best-effort, never raises."""
    try:
        now = datetime.now(UTC).isoformat()
        conn = sqlite3.connect(db_path, timeout=timeout)
        try:
            conn.execute(
                """INSERT INTO session_heartbeats
                   (cc_session_id, source_tag, model, topic, user_summary,
                    genesis_summary, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cc_session_id) DO UPDATE SET
                     source_tag = excluded.source_tag,
                     model = COALESCE(excluded.model, session_heartbeats.model),
                     topic = COALESCE(excluded.topic, session_heartbeats.topic),
                     user_summary = COALESCE(excluded.user_summary,
                                             session_heartbeats.user_summary),
                     genesis_summary = COALESCE(excluded.genesis_summary,
                                                session_heartbeats.genesis_summary),
                     updated_at = excluded.updated_at""",
                (cc_session_id, source_tag, model, topic, user_summary,
                 genesis_summary, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Best-effort — never block the hook. But LOG it: every path in this
        # feature swallows, and the only other observable is an elapsed-time
        # metric, which looks healthy whether the write landed or not. Without
        # this line a heartbeat that fails on every call is indistinguishable
        # from one that works. debug-level so it costs nothing in normal runs.
        logger.debug("session heartbeat upsert failed", exc_info=True)


def _active_filter(exclude_session: str | None) -> tuple[str, list]:
    """The "currently active" predicate, in ONE place.

    Shared by the row read and the count so the two cannot drift. A count that
    applies a different cutoff or forgets the self-exclusion does not report a
    smaller number — it reports a WRONG one, while looking like a denominator.
    """
    cutoff = (datetime.now(UTC) - _STALE_THRESHOLD).isoformat()
    sql = " FROM session_heartbeats WHERE updated_at > ?"
    params: list = [cutoff]
    if exclude_session:
        sql += " AND cc_session_id != ?"
        params.append(exclude_session)
    return sql, params


def get_active_sync(
    db_path: str,
    *,
    exclude_session: str | None = None,
    timeout: float = 1.0,
    limit: int | None = None,
) -> list[dict]:
    """Sync read of active heartbeats for hooks. Returns [] on any error.

    ``limit`` bounds the row count AT THE QUERY. The bound belongs here rather
    than at the caller because there is exactly one caller — the proactive
    memory hook's peer-awareness block (verified with Serena
    ``find_referencing_symbols`` 2026-09-10; the two other files mentioning this
    function do so in comments) — so query and consumer have identical blast
    radius, and a row read only to be discarded is work nobody wants.

    A caller that limits MUST report what it dropped, because ``ORDER BY
    updated_at DESC`` keeps the most recent peers and a silently short list reads
    exactly like "few concurrent sessions" — the same invisible failure the
    empty-list comment below is about. Get that number from
    :func:`count_active_sync`, NOT by reading one row past the limit: a read
    whose result count equals its limit is truncated, so "how many more" derived
    from it saturates at one and states a precise, wrong total.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            where, params = _active_filter(exclude_session)
            sql = (
                "SELECT cc_session_id, source_tag, model, topic, "
                "user_summary, genesis_summary, updated_at" + where + " ORDER BY updated_at DESC"
            )
            if limit is not None:
                # `max(0, ...)`, never `if limit >= 0` — treating a negative
                # limit as "no limit" is fail-OPEN in the one parameter whose
                # entire purpose is to impose a bound, and it fails silently:
                # the caller asked to be bounded and is handed every row.
                sql += " LIMIT ?"
                params.append(max(0, limit))

            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    except Exception:
        # Same reasoning as the writer above: an empty peer list reads exactly
        # like "no concurrent sessions", so a broken read is invisible.
        logger.debug("session heartbeat read failed", exc_info=True)
        return []


def count_active_sync(
    db_path: str,
    *,
    exclude_session: str | None = None,
    timeout: float = 1.0,
) -> int | None:
    """How many peers :func:`get_active_sync` would return UNLIMITED.

    Returns ``None`` — not 0 — when the count cannot be taken. A caller uses this
    to say how many rows its limit hid, and 0 there would claim "nothing hidden"
    on a failed read, which is the fail-open direction: it would under-report
    exactly when something is wrong. ``None`` lets the caller say "some" instead
    of a number it does not have.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=timeout)
        try:
            where, params = _active_filter(exclude_session)
            row = conn.execute("SELECT COUNT(*)" + where, params).fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception:
        logger.debug("session heartbeat count failed", exc_info=True)
        return None
