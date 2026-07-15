"""CRUD for session_charters + session_ledger — the session-manager spine.

``session_id`` throughout is the CC transcript session id (matches
``cc_sessions.cc_session_id``, NOT ``cc_sessions.id``).

Immutability contract: ``origin_prompt``/``origin_ts`` are write-once. Every
origin write is scoped ``WHERE origin_prompt IS NULL`` — ``import_charter``
(INSERT OR IGNORE + stub-fill) here, and the PreCompact hook's own fill
(scripts/genesis_precompact.py). No general UPDATE ever lists origin columns.

Callers pass the shared SerializedConnection: commit on every write, never
rollback (a rollback on the shared connection would clobber concurrent
writers' uncommitted work).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite

VALID_LEDGER_STATUSES = frozenset({"open", "in_progress", "done", "absorbed", "dropped"})
VALID_ADDED_BY = frozenset({"foreground", "ambient", "pulse"})

# Living-field bounds (enforced here so every writer shares them)
MAX_POINTERS = 12
MAX_POINTER_CHARS = 300
MAX_MISSION_CHARS = 1000
MAX_LEDGER_TEXT_CHARS = 1000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _decode_pointers(row: dict) -> dict:
    """JSON-decode the pointers column in place (tolerant of bad data)."""
    try:
        row["pointers"] = json.loads(row.get("pointers") or "[]")
    except (json.JSONDecodeError, TypeError):
        row["pointers"] = []
    return row


async def upsert_stub(db: aiosqlite.Connection, session_id: str) -> None:
    """Ensure a charter row exists so living-field writes can precede the
    session's first compaction. Origin stays NULL until the PreCompact hook
    fills it from the transcript head."""
    await db.execute(
        """INSERT OR IGNORE INTO session_charters
           (session_id, pointers, compaction_count, created_at)
           VALUES (?, '[]', 0, ?)""",
        (session_id, _now_iso()),
    )
    await db.commit()


async def import_charter(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    origin_prompt: str,
    origin_ts: str | None,
    transcript_path: str | None = None,
    mission: str | None = None,
    pointers: list[str] | None = None,
    compaction_count: int = 0,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> str:
    """Backfill entry point (charter.json → DB). Returns one of:

    - "imported": no row existed — full INSERT.
    - "origin_filled": a stub row existed with NULL origin (an MCP write
      preceded the backfill) — origin_prompt/origin_ts (+ transcript_path)
      filled via WHERE origin_prompt IS NULL, mission/pointers/ledger edits
      preserved. Without this, a stubbed legacy session would lose its
      charter injection until its next compaction (Codex P2, PR #1053).
    - "skipped": row exists with origin already set — nothing changes, so a
      re-run after MCP edits is a no-op.
    """
    cursor = await db.execute(
        """INSERT OR IGNORE INTO session_charters
           (session_id, transcript_path, origin_prompt, origin_ts, mission,
            pointers, compaction_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            transcript_path,
            origin_prompt,
            origin_ts,
            mission,
            json.dumps(pointers or []),
            compaction_count,
            created_at or _now_iso(),
            updated_at,
        ),
    )
    if cursor.rowcount > 0:
        await db.commit()
        return "imported"
    cursor = await db.execute(
        """UPDATE session_charters SET origin_prompt = ?, origin_ts = ?,
           transcript_path = COALESCE(transcript_path, ?), updated_at = ?
           WHERE session_id = ? AND origin_prompt IS NULL""",
        (origin_prompt, origin_ts, transcript_path, _now_iso(), session_id),
    )
    await db.commit()
    return "origin_filled" if cursor.rowcount > 0 else "skipped"


async def get(db: aiosqlite.Connection, session_id: str) -> dict | None:
    """Charter row as a dict (pointers JSON-decoded), or None. Exact-id
    lookup — resolve truncated ids via resolve_session_id first."""
    cursor = await db.execute("SELECT * FROM session_charters WHERE session_id = ?", (session_id,))
    row = await cursor.fetchone()
    return _decode_pointers(dict(row)) if row else None


async def resolve_session_id(db: aiosqlite.Connection, session_id: str) -> str:
    """Resolve a truncated session id to the full one by unique prefix match.

    Full-length ids (>= 32 chars) pass through unchanged. Prefixes are
    matched against session_charters first, then against
    cc_sessions.cc_session_id — the latter covers sessions that have not
    chartered yet (pre-first-compaction), so a stub is never created under a
    truncated id that later diverges from the hook's full id (Codex P2,
    PR #1053). Ambiguous or unmatched prefixes return the input unchanged;
    WRITE callers must refuse to create rows for unresolved short ids.
    """
    sid = (session_id or "").strip()
    if len(sid) >= 32 or not sid:
        return sid
    cursor = await db.execute(
        "SELECT session_id FROM session_charters WHERE session_id LIKE ? LIMIT 2",
        (sid + "%",),
    )
    rows = await cursor.fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        cursor = await db.execute(
            "SELECT DISTINCT cc_session_id FROM cc_sessions WHERE cc_session_id LIKE ? LIMIT 2",
            (sid + "%",),
        )
        rows = await cursor.fetchall()
        if len(rows) == 1:
            return rows[0][0]
    return sid


async def set_mission(db: aiosqlite.Connection, session_id: str, mission: str | None) -> bool:
    """Set/clear the living mission. Never touches origin columns."""
    if mission is not None:
        mission = mission.strip()[:MAX_MISSION_CHARS] or None
    cursor = await db.execute(
        "UPDATE session_charters SET mission = ?, updated_at = ? WHERE session_id = ?",
        (mission, _now_iso(), session_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_pointers(db: aiosqlite.Connection, session_id: str, pointers: list[str]) -> bool:
    """Whole-list pointer write (callers do the read-modify-write; the MCP
    tool serializes through the shared connection). Caps enforced here."""
    cleaned = [str(p).strip()[:MAX_POINTER_CHARS] for p in pointers if str(p).strip()]
    cleaned = cleaned[:MAX_POINTERS]
    cursor = await db.execute(
        "UPDATE session_charters SET pointers = ?, updated_at = ? WHERE session_id = ?",
        (json.dumps(cleaned), _now_iso(), session_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def ledger_add(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    text: str,
    source_ref: str | None = None,
    added_by: str = "foreground",
) -> str:
    """Add an open ledger item and return its id."""
    if added_by not in VALID_ADDED_BY:
        raise ValueError(f"invalid added_by: {added_by!r}")
    text = text.strip()[:MAX_LEDGER_TEXT_CHARS]
    if not text:
        raise ValueError("ledger text must be non-empty")
    item_id = _new_id()
    await db.execute(
        """INSERT INTO session_ledger
           (id, session_id, text, status, source_ref, added_by, created_at)
           VALUES (?, ?, ?, 'open', ?, ?, ?)""",
        (item_id, session_id, text, source_ref, added_by, _now_iso()),
    )
    await db.commit()
    return item_id


async def ledger_update(
    db: aiosqlite.Connection,
    item_id: str,
    *,
    status: str | None = None,
    text: str | None = None,
    evidence: str | None = None,
) -> bool:
    """Update a ledger item's living fields. Returns False for unknown ids."""
    if status is not None and status not in VALID_LEDGER_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    sets: list[str] = ["updated_at = ?"]
    params: list[object] = [_now_iso()]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if text is not None:
        text = text.strip()[:MAX_LEDGER_TEXT_CHARS]
        if not text:
            raise ValueError("ledger text must be non-empty")
        sets.append("text = ?")
        params.append(text)
    if evidence is not None:
        sets.append("evidence = ?")
        params.append(evidence)
    params.append(item_id)
    cursor = await db.execute(
        f"UPDATE session_ledger SET {', '.join(sets)} WHERE id = ?",  # noqa: S608 — column names from a literal allow-list above
        params,
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_ledger_item(db: aiosqlite.Connection, item_id: str) -> dict | None:
    """Single ledger row as a dict, or None for unknown ids."""
    cursor = await db.execute("SELECT * FROM session_ledger WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def ledger_list(
    db: aiosqlite.Connection,
    session_id: str,
    statuses: list[str] | None = None,
) -> list[dict]:
    """Ledger items for a session, oldest first; optional status filter."""
    query = "SELECT * FROM session_ledger WHERE session_id = ?"
    params: list[object] = [session_id]
    if statuses:
        invalid = set(statuses) - VALID_LEDGER_STATUSES
        if invalid:
            raise ValueError(f"invalid statuses: {sorted(invalid)}")
        placeholders = ", ".join("?" for _ in statuses)
        query += f" AND status IN ({placeholders})"  # noqa: S608 — placeholders only
        params.extend(statuses)
    query += " ORDER BY created_at"
    cursor = await db.execute(query, params)
    return [dict(row) for row in await cursor.fetchall()]


async def ledger_all(
    db: aiosqlite.Connection,
    *,
    limit: int = 10000,
) -> list[dict]:
    """All ledger rows across sessions, oldest first (incl. added_by).

    Read seam for the shadow precision report (leak-invariant check needs
    the added_by column across every session). Assumes a Row factory.
    """
    lim = max(1, min(int(limit), 100000))
    cursor = await db.execute(
        "SELECT * FROM session_ledger ORDER BY created_at ASC LIMIT ?", (lim,)
    )
    return [dict(r) for r in await cursor.fetchall()]


async def ledger_counts(db: aiosqlite.Connection, session_id: str) -> dict[str, int]:
    """Per-status row counts for a session's ledger (absent statuses omitted)."""
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM session_ledger WHERE session_id = ? GROUP BY status",
        (session_id,),
    )
    return {row[0]: row[1] for row in await cursor.fetchall()}
