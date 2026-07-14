"""CRUD operations for cc_sessions table."""

from __future__ import annotations

import json
import logging

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    session_type: str,
    model: str,
    started_at: str,
    last_activity_at: str,
    effort: str = "medium",
    status: str = "active",
    user_id: str | None = None,
    channel: str | None = None,
    pid: int | None = None,
    source_tag: str = "foreground",
    metadata: str | None = None,
    thread_id: str | None = None,
    origin_class: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO cc_sessions
           (id, session_type, user_id, channel, model, effort, status,
            pid, started_at, last_activity_at, source_tag, metadata, thread_id,
            origin_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, session_type, user_id, channel, model, effort, status,
         pid, started_at, last_activity_at, source_tag, metadata, thread_id,
         origin_class),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM cc_sessions WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_active_foreground(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    channel: str,
    thread_id: str | None = None,
) -> dict | None:
    if thread_id is not None:
        cursor = await db.execute(
            """SELECT * FROM cc_sessions
               WHERE session_type = 'foreground'
                 AND status = 'active'
                 AND user_id = ?
                 AND channel = ?
                 AND thread_id = ?
               ORDER BY started_at DESC LIMIT 1""",
            (user_id, channel, thread_id),
        )
    else:
        cursor = await db.execute(
            """SELECT * FROM cc_sessions
               WHERE session_type = 'foreground'
                 AND status = 'active'
                 AND user_id = ?
                 AND channel = ?
                 AND thread_id IS NULL
               ORDER BY started_at DESC LIMIT 1""",
            (user_id, channel),
        )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_status(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE cc_sessions SET status = ? WHERE id = ?",
        (status, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_activity(
    db: aiosqlite.Connection,
    id: str,
    *,
    last_activity_at: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE cc_sessions SET last_activity_at = ? WHERE id = ?",
        (last_activity_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def query_active(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM cc_sessions WHERE status = 'active' ORDER BY started_at DESC",
    )
    return [dict(r) for r in await cursor.fetchall()]


async def query_stale(
    db: aiosqlite.Connection,
    *,
    older_than: str,
) -> list[dict]:
    """Return active non-foreground sessions older than *older_than*.

    Foreground sessions are excluded at the SQL level — they should never
    be auto-expired (the user might resume).
    """
    cursor = await db.execute(
        """SELECT * FROM cc_sessions
           WHERE status = 'active'
             AND session_type != 'foreground'
             AND last_activity_at < ?
           ORDER BY last_activity_at ASC""",
        (older_than,),
    )
    return [dict(r) for r in await cursor.fetchall()]


# reap_stale (bulk UPDATE → 'completed') was deleted. It relabeled
# crashed/orphaned sessions as successes; the session reaper now routes
# through ``SessionManager.cleanup_stale()`` (policy-aware, → 'expired',
# fires end-hooks). See runtime/init/learning.py::_reap_stale_sessions.


async def set_pid(db: aiosqlite.Connection, id: str, pid: int) -> bool:
    """Write the subprocess PID to an existing cc_sessions row."""
    cursor = await db.execute(
        "UPDATE cc_sessions SET pid = ? WHERE id = ?", (pid, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_cc_session_id(
    db: aiosqlite.Connection,
    id: str,
    *,
    cc_session_id: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE cc_sessions SET cc_session_id = ? WHERE id = ?",
        (cc_session_id, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def clear_cc_session_id(
    db: aiosqlite.Connection,
    id: str,
) -> bool:
    """Set cc_session_id to NULL for a given session (stale resume recovery)."""
    cursor = await db.execute(
        "UPDATE cc_sessions SET cc_session_id = NULL WHERE id = ?",
        (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def merge_metadata(
    db: aiosqlite.Connection,
    id: str,
    patch: dict,
) -> bool:
    """Shallow read-merge-write a patch into the session's JSON ``metadata``.

    Used to persist roster endpoint context (model-diversification) without a
    migration. Tolerates absent/corrupt existing metadata (treats as ``{}``).
    Returns True if the row exists and was updated.
    """
    cursor = await db.execute(
        "SELECT metadata FROM cc_sessions WHERE id = ?", (id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return False
    existing: dict = {}
    if row[0]:
        try:
            loaded = json.loads(row[0])
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, TypeError):
            existing = {}
    existing.update(patch)
    cursor = await db.execute(
        "UPDATE cc_sessions SET metadata = ? WHERE id = ?",
        (json.dumps(existing), id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_model_effort(
    db: aiosqlite.Connection,
    id: str,
    *,
    model: str | None = None,
    effort: str | None = None,
) -> bool:
    """Update model and/or effort on an existing session."""
    updates = []
    params: list = []
    if model is not None:
        updates.append("model = ?")
        params.append(model)
    if effort is not None:
        updates.append("effort = ?")
        params.append(effort)
    if not updates:
        return False
    params.append(id)
    cursor = await db.execute(
        f"UPDATE cc_sessions SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    await db.commit()
    return cursor.rowcount > 0


async def query_by_skill_tag(
    db: aiosqlite.Connection,
    *,
    skill_tag: str,
    status: str | None = None,
) -> list[dict]:
    """Find sessions tagged with a specific skill."""
    if status:
        cursor = await db.execute(
            """SELECT * FROM cc_sessions
               WHERE metadata LIKE ? AND status = ?
               ORDER BY started_at DESC""",
            (f'%"{skill_tag}"%', status),
        )
    else:
        cursor = await db.execute(
            """SELECT * FROM cc_sessions
               WHERE metadata LIKE ?
               ORDER BY started_at DESC""",
            (f'%"{skill_tag}"%',),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def get_by_session_types(
    db: aiosqlite.Connection,
    session_types: set[str],
) -> list[dict]:
    """Return all sessions whose session_type is in the given set.

    Used by the skill-effectiveness baseline computation, which then filters
    in Python on skill_tags membership. Returns [] for an empty input.
    """
    types = list(session_types)
    if not types:
        return []
    placeholders = ",".join("?" * len(types))
    cursor = await db.execute(
        f"SELECT * FROM cc_sessions WHERE session_type IN ({placeholders})",  # noqa: S608
        (*types,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def update_rate_limit(
    db: aiosqlite.Connection,
    id: str,
    *,
    rate_limited_at: str,
    rate_limit_resumes_at: str | None = None,
) -> bool:
    cursor = await db.execute(
        """UPDATE cc_sessions
           SET rate_limited_at = ?, rate_limit_resumes_at = ?
           WHERE id = ?""",
        (rate_limited_at, rate_limit_resumes_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def increment_cost(
    db: aiosqlite.Connection,
    id: str,
    *,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> bool:
    """Add cost and token counts to an existing session (incremental)."""
    cursor = await db.execute(
        """UPDATE cc_sessions
           SET cost_usd = COALESCE(cost_usd, 0) + ?,
               input_tokens = COALESCE(input_tokens, 0) + ?,
               output_tokens = COALESCE(output_tokens, 0) + ?
           WHERE id = ?""",
        (cost_usd, input_tokens, output_tokens, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM cc_sessions WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0


# -- Extraction helpers -------------------------------------------------------


async def get_all_cc_session_ids(db: aiosqlite.Connection) -> set[str]:
    """Return set of all non-null cc_session_id values."""
    cursor = await db.execute(
        "SELECT cc_session_id FROM cc_sessions WHERE cc_session_id IS NOT NULL"
    )
    return {row[0] for row in await cursor.fetchall()}


async def register_from_filesystem(
    db: aiosqlite.Connection,
    *,
    id: str,
    cc_session_id: str,
    started_at: str,
) -> bool:
    """Auto-register a session discovered from filesystem transcripts.

    Uses INSERT OR IGNORE so duplicates are silently skipped.
    Returns True if a new row was inserted.
    """
    cursor = await db.execute(
        "INSERT OR IGNORE INTO cc_sessions "
        "(id, cc_session_id, session_type, model, source_tag, "
        " status, started_at, last_activity_at) "
        "VALUES (?, ?, 'foreground', 'unknown', 'foreground', "
        " 'completed', ?, ?)",
        (id, cc_session_id, started_at, started_at),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_extractable(
    db: aiosqlite.Connection,
    *,
    source_tags: tuple[str, ...],
    statuses: tuple[str, ...] = ("active", "completed", "checkpointed"),
) -> list[dict]:
    """Query sessions eligible for memory extraction."""
    tag_ph = ",".join("?" for _ in source_tags)
    status_ph = ",".join("?" for _ in statuses)
    cursor = await db.execute(
        f"SELECT id, cc_session_id, source_tag, last_extracted_at, "
        f"       last_extracted_line, started_at "
        f"FROM cc_sessions "
        f"WHERE source_tag IN ({tag_ph}) "
        f"  AND status IN ({status_ph}) "
        f"ORDER BY started_at DESC",
        (*source_tags, *statuses),
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in await cursor.fetchall()]


async def update_extraction_watermark(
    db: aiosqlite.Connection,
    id: str,
    *,
    last_extracted_line: int,
    last_extracted_at: str,
) -> bool:
    """Update the extraction watermark for a session."""
    cursor = await db.execute(
        "UPDATE cc_sessions SET last_extracted_at = ?, last_extracted_line = ? "
        "WHERE id = ?",
        (last_extracted_at, last_extracted_line, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_keywords(db: aiosqlite.Connection, id: str) -> str | None:
    """Get the keywords string for a session."""
    cursor = await db.execute(
        "SELECT keywords FROM cc_sessions WHERE id = ?", (id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def update_topic_and_keywords(
    db: aiosqlite.Connection,
    id: str,
    *,
    topic: str,
    keywords: str,
) -> bool:
    """Update the session topic and keywords index."""
    cursor = await db.execute(
        "UPDATE cc_sessions SET topic = ?, keywords = ? WHERE id = ?",
        (topic, keywords, id),
    )
    await db.commit()
    return cursor.rowcount > 0


# -- Aggregate helpers (morning report, etc.) ---------------------------------


async def get_status_counts(
    db: aiosqlite.Connection,
    *,
    hours: int = 24,
) -> dict[str, int]:
    """Count sessions by status within the given time window."""
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM cc_sessions "
        "WHERE started_at >= datetime('now', ? || ' hours') "
        "GROUP BY status",
        (f"-{hours}",),
    )
    return {row[0]: row[1] for row in await cursor.fetchall()}


async def get_recent_topics(
    db: aiosqlite.Connection,
    *,
    hours: int = 24,
    session_type: str = "foreground",
    limit: int = 15,
) -> list[str]:
    """Get recent non-empty session topics."""
    cursor = await db.execute(
        "SELECT topic FROM cc_sessions "
        "WHERE started_at >= datetime('now', ? || ' hours') "
        "AND session_type = ? "
        "AND topic != '' AND topic IS NOT NULL "
        "ORDER BY started_at DESC LIMIT ?",
        (f"-{hours}", session_type, limit),
    )
    return [row[0] for row in await cursor.fetchall()]


async def any_external_session_overlapping(
    db: aiosqlite.Connection,
    *,
    since_iso: str,
    end_iso: str,
) -> bool:
    """True iff any ``external_untrusted`` session's lifespan OVERLAPS the
    window [since_iso, end_iso] (WS-3 gate-2 run-level provenance aggregate).

    Reflection runs are ABOUT a tick window, not one session — the reflection
    context carries no per-session refs, so the honest granularity is: did any
    external-origin session overlap the material window?

    Overlap, not a point test on ``last_activity_at``: that column is set at
    creation for background sessions and only advanced by the foreground
    ``update_activity`` path, so a long-running inbox/direct session that
    STARTED before the window but is still active (or completed) inside it
    would have a stale ``last_activity_at`` and be missed by a naive
    ``last_activity_at >= since`` filter. A session overlaps iff it started
    at/before the window end AND either it is still active OR its last
    recorded activity is at/after the window start. Conservative direction:
    over-tag external (a still-active external session always counts), never
    under-tag. NULL ``origin_class`` rows (foreground / pre-substrate /
    first-party dispatch) never match.
    """
    cursor = await db.execute(
        """SELECT 1 FROM cc_sessions
           WHERE origin_class = 'external_untrusted'
             AND started_at <= ?
             AND (status = 'active' OR last_activity_at >= ?)
           LIMIT 1""",
        (end_iso, since_iso),
    )
    return await cursor.fetchone() is not None


# Material window for the reflection-run provenance aggregate. Light
# reflections run on an adaptive cadence well under an hour; 60 minutes
# over-covers deliberately (conservative direction: over-tag external, never
# under-tag). Shadow data will show whether this needs tightening.
REFLECTION_ORIGIN_WINDOW_MINUTES = 60

_logger = logging.getLogger(__name__)


async def reflection_window_origin(db: aiosqlite.Connection, *, end_iso: str) -> str:
    """Run-level origin aggregate for a reflection ending at ``end_iso``.

    Returns ``"external_untrusted"`` iff any external-origin session was
    active in the material window, else ``"first_party"``. NEVER raises —
    reflection delta writes must not break on a provenance lookup; on any
    error it defaults to first_party (shadow-safe: gate-2 self-guards to no
    row for first_party, and nothing enforces on this value).
    """
    try:
        from datetime import datetime, timedelta

        since = (
            datetime.fromisoformat(end_iso)
            - timedelta(minutes=REFLECTION_ORIGIN_WINDOW_MINUTES)
        ).isoformat()
        if await any_external_session_overlapping(
            db, since_iso=since, end_iso=end_iso
        ):
            return "external_untrusted"
    except Exception:
        _logger.debug(
            "reflection_window_origin failed; defaulting first_party", exc_info=True
        )
    return "first_party"
