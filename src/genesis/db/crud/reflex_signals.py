"""CRUD for reflex_signals — fingerprint-deduped task.failed signals.

One row per failure fingerprint; recurrences increment ``occurrence_count``
via ``ON CONFLICT(fingerprint) DO UPDATE`` (a burst of N identical crashes
is one signal, not N). ``maybe_reopen`` is the caller-side policy step: a
recurrence of a terminal signal (merged / dismissed / expired / failed)
past its mute window flips it back to ``new`` — for a ``merged`` signal
that recurrence is evidence the fix did not hold.

Callers pass the shared SerializedConnection: commit on every write, never
rollback. All timestamps are injected ISO-UTC strings (lexicographic
comparison is used for the mute window) — no wall clock in this module.
Reads build dicts from an explicit column list rather than relying on
``row_factory`` (the shared connection's factory is not guaranteed).
"""

from __future__ import annotations

import uuid

import aiosqlite

_COLS = (
    "id",
    "fingerprint",
    "class_key",
    "task_name",
    "subsystem",
    "error_type",
    "last_error_message",
    "traceback_tail",
    "status",
    "occurrence_count",
    "first_seen_at",
    "last_seen_at",
    "reopen_count",
    "reopened_at",
    "muted_until",
    "active_diagnosis_id",
    "diagnose_request_id",
    "fix_request_id",
    "task_id",
    "pr_url",
    "outcome_label",
    "created_at",
    "updated_at",
)

_SELECT = f"SELECT {', '.join(_COLS)} FROM reflex_signals"  # noqa: S608 — static column list

# Statuses a recurrence may reopen from. Everything else is an ACTIVE
# lifecycle stage (carded / diagnosing / fixing / in PR) where the
# recurrence is expected — the occurrence counter still climbs.
_REOPENABLE = (
    "merged",
    "resolved",
    "dismissed_notbug",
    "dismissed_wontfix",
    "card_expired",
    "diagnose_failed",
    "fix_failed",
)


def _row_to_dict(row: tuple) -> dict:
    return dict(zip(_COLS, row, strict=True))


async def upsert_occurrence(
    db: aiosqlite.Connection,
    *,
    fingerprint: str,
    class_key: str,
    task_name: str,
    subsystem: str,
    error_type: str,
    error_message: str | None,
    traceback_tail: str | None,
    now: str,
) -> dict:
    """Insert a new signal or record one more occurrence of a known one.

    The conflict path deliberately touches only occurrence bookkeeping
    (count, last_seen, latest message) — never ``status``: lifecycle
    transitions belong to ``set_status``/``maybe_reopen``. Returns the row
    after the write.
    """
    await db.execute(
        """INSERT INTO reflex_signals
           (id, fingerprint, class_key, task_name, subsystem, error_type,
            last_error_message, traceback_tail, status, occurrence_count,
            first_seen_at, last_seen_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', 1, ?, ?, ?, ?)
           ON CONFLICT(fingerprint) DO UPDATE SET
             occurrence_count = occurrence_count + 1,
             last_seen_at = excluded.last_seen_at,
             last_error_message = excluded.last_error_message,
             task_name = excluded.task_name,
             updated_at = excluded.updated_at""",
        (
            uuid.uuid4().hex[:16],
            fingerprint,
            class_key,
            task_name,
            subsystem,
            error_type,
            error_message,
            traceback_tail,
            now,
            now,
            now,
            now,
        ),
    )
    await db.commit()
    row = await get_by_fingerprint(db, fingerprint)
    if row is None:  # pragma: no cover — row was just upserted above
        raise RuntimeError(f"reflex_signals upsert vanished for fingerprint {fingerprint}")
    return row


async def maybe_reopen(db: aiosqlite.Connection, *, fingerprint: str, now: str) -> bool:
    """Reopen a terminal signal whose mute window has passed.

    Guarded single UPDATE: only fires when status is reopenable AND
    ``muted_until`` is NULL or in the past. Clears the per-round lifecycle
    references so the next card round starts clean. Returns True iff the
    signal was reopened.
    """
    placeholders = ", ".join("?" * len(_REOPENABLE))
    cursor = await db.execute(
        f"""UPDATE reflex_signals SET
              status = 'new',
              reopen_count = reopen_count + 1,
              reopened_at = ?,
              diagnose_request_id = NULL,
              fix_request_id = NULL,
              task_id = NULL,
              updated_at = ?
            WHERE fingerprint = ?
              AND status IN ({placeholders})
              AND (muted_until IS NULL OR muted_until <= ?)""",  # noqa: S608 — static placeholders
        (now, now, fingerprint, *_REOPENABLE, now),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_status(
    db: aiosqlite.Connection,
    *,
    signal_id: str,
    expected_from: str,
    to: str,
    now: str,
) -> bool:
    """Guarded lifecycle transition — no-op unless status == expected_from."""
    cursor = await db.execute(
        "UPDATE reflex_signals SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (to, now, signal_id, expected_from),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_by_fingerprint(db: aiosqlite.Connection, fingerprint: str) -> dict | None:
    cursor = await db.execute(f"{_SELECT} WHERE fingerprint = ?", (fingerprint,))
    row = await cursor.fetchone()
    return _row_to_dict(tuple(row)) if row is not None else None


async def list_by_status(db: aiosqlite.Connection, status: str, *, limit: int = 100) -> list[dict]:
    cursor = await db.execute(
        f"{_SELECT} WHERE status = ? ORDER BY last_seen_at DESC LIMIT ?", (status, limit)
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(tuple(r)) for r in rows]


async def list_recent(db: aiosqlite.Connection, *, limit: int = 10) -> list[dict]:
    """Most-recently-seen signals across all statuses, newest first."""
    cursor = await db.execute(f"{_SELECT} ORDER BY last_seen_at DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    return [_row_to_dict(tuple(r)) for r in rows]


# ── observability aggregates (PR1.5) — shared by the reflex_status MCP tool
#    and the in-server health snapshot; read-only ────────────────────────────


async def count_by_status(db: aiosqlite.Connection) -> dict[str, int]:
    """Signal counts keyed by lifecycle status (statuses with zero rows omitted)."""
    cursor = await db.execute("SELECT status, COUNT(*) FROM reflex_signals GROUP BY status")
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def top_class_keys(db: aiosqlite.Connection, *, limit: int = 8) -> list[dict]:
    """Class keys ranked by distinct-signal count, occurrence volume as tiebreak.

    Signal count leads (how many distinct bugs in this class), occurrence sum
    breaks ties (how loud they are) — the ordering the §7.2 taxonomy work reads.
    """
    cursor = await db.execute(
        """SELECT class_key, COUNT(*) AS signals, SUM(occurrence_count) AS occurrences
           FROM reflex_signals
           GROUP BY class_key
           ORDER BY signals DESC, occurrences DESC
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [{"class_key": row[0], "signals": row[1], "occurrences": row[2]} for row in rows]
