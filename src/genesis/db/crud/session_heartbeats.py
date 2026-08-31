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
        pass  # Best-effort — never block the hook


def get_active_sync(
    db_path: str,
    *,
    exclude_session: str | None = None,
    timeout: float = 1.0,
) -> list[dict]:
    """Sync read of active heartbeats for hooks. Returns [] on any error."""
    try:
        cutoff = (datetime.now(UTC) - _STALE_THRESHOLD).isoformat()
        conn = sqlite3.connect(db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
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

            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []
