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
#
# The identity columns (roster): `cwd`/`git_branch`/`slot` follow the same
# COALESCE contract (git_branch three-valued: ""=known none, NULL=unknown).
# `pid` + `pid_started_at` move as ONE PAIR via a CASE on excluded.pid --
# pid_started_at exists solely to reject a recycled pid at observe time, so a
# (pid, started_at) assembled from two different writes would attribute one
# process's start time to another, worse than unknown. A write that knows the
# pid updates both; a write that does not (None) touches neither. Liveness
# itself is never stored -- readers observe /proc at read time.

_UPSERT_SQL = """INSERT INTO session_heartbeats
   (cc_session_id, source_tag, model, topic, user_summary,
    genesis_summary, updated_at, pid, pid_started_at, cwd, git_branch, slot)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
   ON CONFLICT(cc_session_id) DO UPDATE SET
     source_tag = excluded.source_tag,
     model = COALESCE(excluded.model, session_heartbeats.model),
     topic = COALESCE(excluded.topic, session_heartbeats.topic),
     user_summary = COALESCE(excluded.user_summary,
                             session_heartbeats.user_summary),
     genesis_summary = COALESCE(excluded.genesis_summary,
                                session_heartbeats.genesis_summary),
     updated_at = excluded.updated_at,
     pid = COALESCE(excluded.pid, session_heartbeats.pid),
     pid_started_at = CASE WHEN excluded.pid IS NOT NULL
                           THEN excluded.pid_started_at
                           ELSE session_heartbeats.pid_started_at END,
     cwd = COALESCE(excluded.cwd, session_heartbeats.cwd),
     git_branch = COALESCE(excluded.git_branch, session_heartbeats.git_branch),
     slot = COALESCE(excluded.slot, session_heartbeats.slot)"""


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
    pid: int | None = None,
    pid_started_at: str | None = None,
    cwd: str | None = None,
    git_branch: str | None = None,
    slot: str | None = None,
) -> None:
    """Write or update a session heartbeat."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        _UPSERT_SQL,
        (cc_session_id, source_tag, model, topic, user_summary,
         genesis_summary, now, pid, pid_started_at, cwd, git_branch, slot),
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


# Roster reads: a 24h window of ALL columns. Freshness is a RENDERED attribute
# (observe() computes idle/liveness at read time), never a query filter — the
# 10-min get_active window stays only for legacy callers.
#
# TWO select shapes, deliberately: on a DB that predates the identity migration
# the full SELECT raises "no such column", and this module's swallow-everything
# posture turned that into an EMPTY roster — every peer invisible, reading
# exactly like "no concurrent sessions" (measured live, 2026-09-05). The
# mid-deploy window (hooks updated at session start, DB migrated at server
# restart) puts every install there, so the read degrades to the legacy column
# set with identity fields as None. Pinned by
# test_roster_reads_degrade_on_premigration_schema.
_IDENTITY_COLUMNS = ("pid", "pid_started_at", "cwd", "git_branch", "slot")
_ROSTER_SQL = (
    "SELECT cc_session_id, source_tag, model, topic, user_summary, "
    "genesis_summary, updated_at, pid, pid_started_at, cwd, git_branch, slot "
    "FROM session_heartbeats WHERE updated_at > ?"
)
_ROSTER_SQL_LEGACY = (
    "SELECT cc_session_id, source_tag, model, topic, user_summary, "
    "genesis_summary, updated_at "
    "FROM session_heartbeats WHERE updated_at > ?"
)


def _fill_identity(row: dict) -> dict:
    for col in _IDENTITY_COLUMNS:
        row.setdefault(col, None)
    return row


def _dedupe_by_pid(rows: list[dict]) -> list[dict]:
    """One row per pid — the NEWEST. A /clear starts a new cc_session_id in
    the SAME claude process, so the old conversation's row keeps a live,
    start-time-verified pid for up to 24h and would render as a phantom live
    peer (review finding). One process hosts one current conversation; rows
    are already updated_at DESC, so first-seen wins. pid-less rows exempt."""
    seen: set[int] = set()
    out = []
    for r in rows:
        pid = r.get("pid")
        if pid is not None:
            if pid in seen:
                continue
            seen.add(pid)
        out.append(r)
    return out


async def get_roster(
    db: aiosqlite.Connection,
    *,
    exclude_session: str | None = None,
    window_hours: int = 24,
) -> list[dict]:
    """All heartbeat rows in the window, identity columns included.

    GROUNDWORK(session-roster-pr2): no callers yet — the MCP roster tool and
    the dashboard join consume this async twin in the next batch; the sync
    twin below is live in the [Concurrent] renderer now.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
    for base_sql in (_ROSTER_SQL, _ROSTER_SQL_LEGACY):
        sql, params = base_sql, [cutoff]
        if exclude_session:
            sql += " AND cc_session_id != ?"
            params.append(exclude_session)
        sql += " ORDER BY updated_at DESC"
        try:
            cursor = await db.execute(sql, params)
        except Exception as exc:
            if "no such column" in str(exc).lower():
                continue  # pre-migration schema: degrade to the legacy shape
            raise
        return _dedupe_by_pid(
            [_fill_identity(dict(row)) for row in await cursor.fetchall()]
        )
    return []


def get_roster_sync(
    db_path: str,
    *,
    exclude_session: str | None = None,
    window_hours: int = 24,
    timeout: float = 1.0,
) -> list[dict]:
    """Sync twin of get_roster for hooks. Returns [] on any error."""
    try:
        cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
        conn = sqlite3.connect(db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            for base_sql in (_ROSTER_SQL, _ROSTER_SQL_LEGACY):
                sql, params = base_sql, [cutoff]
                if exclude_session:
                    sql += " AND cc_session_id != ?"
                    params.append(exclude_session)
                sql += " ORDER BY updated_at DESC"
                try:
                    rows = conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such column" in str(exc).lower():
                        continue  # pre-migration schema: degrade, don't vanish
                    raise
                return _dedupe_by_pid([_fill_identity(dict(row)) for row in rows])
            return []
        finally:
            conn.close()
    except Exception:
        logger.debug("session roster read failed", exc_info=True)
        return []


async def cleanup_stale(db: aiosqlite.Connection, *, proc_root: str = "/proc") -> int:
    """Delete heartbeats whose session is GONE. Returns count deleted.

    IDLE IS NOT DEAD (owner correction, 2026-09-05): a session sitting
    untouched in a terminal is alive and must keep its row, or the roster's
    IDLE-FOR can never render. A row is deleted only when:
      - its pid is observed dead or recycled (no /proc entry, or comm is not
        "claude" — comm is world-readable, so this works inside the server
        sandbox where environ reads EACCES), for rows past the old window; or
      - it has NO pid (legacy row) and is past the old 10-min window; or
      - nothing touched it for 24h (backstop bounding rows whose pid check
        cannot conclude — a wedged writer must not leak rows forever).
    """
    from pathlib import Path

    cutoff = (datetime.now(UTC) - _STALE_THRESHOLD).isoformat()
    backstop = (datetime.now(UTC) - timedelta(hours=24)).isoformat()

    cursor = await db.execute(
        "SELECT cc_session_id, pid, updated_at FROM session_heartbeats "
        "WHERE updated_at < ?",
        (cutoff,),
    )
    doomed: list[str] = []
    for sid, pid, updated_at in await cursor.fetchall():
        if updated_at < backstop:
            doomed.append(sid)
            continue
        if pid is None:
            doomed.append(sid)  # legacy row: old-window behavior preserved
            continue
        try:
            # bytes compare — comm is attacker-influenced and read_text()'s
            # UnicodeDecodeError is a ValueError, not an OSError; a raise here
            # stalls ALL heartbeat GC (probe-confirmed in review).
            alive = (
                Path(proc_root) / str(pid) / "comm"
            ).read_bytes().strip() == b"claude"
        except OSError:
            alive = False
        if not alive:
            doomed.append(sid)

    for sid in doomed:
        # updated_at re-checked IN the delete: between the scan and here a
        # resumed session (same sid, new pid) may have upserted a fresh row,
        # and an unconditioned delete would destroy it (review TOCTOU find).
        await db.execute(
            "DELETE FROM session_heartbeats "
            "WHERE cc_session_id = ? AND updated_at < ?",
            (sid, cutoff),
        )
    await db.commit()
    return len(doomed)


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
    pid: int | None = None,
    pid_started_at: str | None = None,
    cwd: str | None = None,
    git_branch: str | None = None,
    slot: str | None = None,
    timeout: float = 1.0,
) -> None:
    """Sync heartbeat write for hooks. Best-effort, never raises."""
    try:
        now = datetime.now(UTC).isoformat()
        conn = sqlite3.connect(db_path, timeout=timeout)
        try:
            conn.execute(
                _UPSERT_SQL,
                (cc_session_id, source_tag, model, topic, user_summary,
                 genesis_summary, now, pid, pid_started_at, cwd, git_branch,
                 slot),
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
        # Same reasoning as the writer above: an empty peer list reads exactly
        # like "no concurrent sessions", so a broken read is invisible.
        logger.debug("session heartbeat read failed", exc_info=True)
        return []
