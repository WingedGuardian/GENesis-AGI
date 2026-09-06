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
# "ambient" means a DISPATCHED Claude Code session (see _default_added_by);
# "ambient_ledger_extractor" is the detached Haiku extractor that watches a
# session from the outside. They must stay distinct: the shadow report's leak
# invariant keys on the extractor value to assert it has written nothing live,
# and a shared value would make that check unable to tell them apart on the
# very day it starts mattering. Mirrored by a schema CHECK (the session_ledger_ambient_extractor migration).
VALID_ADDED_BY = frozenset(
    {"foreground", "ambient", "pulse", "ambient_ledger_extractor"}
)
# The subset a CALLER may name. `ambient_ledger_extractor` is INTERNAL
# provenance — the detached worker sets it on its own writes — and it is not an
# input any caller supplies. Leaving it in the one shared allow-list let anyone
# calling the public `session_ledger_add` MCP tool claim that identity, which
# breaks the shadow report's leak invariant precisely: that check asserts the
# extractor has written nothing live, so a caller able to forge the value makes
# a real leak and a forged row indistinguishable.
#
# Two names because they answer two questions — "is this value storable?" and
# "may this value be asked for?" — and the public surface needs the second.
CALLER_SETTABLE_ADDED_BY = frozenset({"foreground", "ambient", "pulse"})

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
    """Set/clear the living mission. Never touches origin columns.

    Stamps ``mission_updated_at`` as well as ``updated_at``. The two are NOT
    interchangeable and the difference is load-bearing: ``updated_at`` is a ROW
    timestamp that ``set_pointers`` and the charter upsert also bump, so it
    cannot answer "when was the mission set" — a pointer edit would make a stale
    founding mission look freshly declared. The concurrent-session peer line
    compares this column against the extraction job's own timestamp to decide
    which topic is the more recent statement of what a session is doing.
    """
    if mission is not None:
        mission = mission.strip()[:MAX_MISSION_CHARS] or None
    now = _now_iso()
    cursor = await db.execute(
        "UPDATE session_charters SET mission = ?, updated_at = ?,"
        " mission_updated_at = ? WHERE session_id = ?",
        (mission, now, now, session_id),
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


def _one_line(text: str) -> str:
    """Ledger text as ONE line, capped. Whitespace runs collapse to one space.

    `.strip()` alone trims only the ENDS, so an embedded newline survived into
    two model-facing renderers that emit one line PER ROW — the charter block
    re-injected into every post-compaction window, and the per-prompt inventory
    tag. A single row then rendered as two, and the second line was
    indistinguishable from a genuine ledger row in Genesis's own voice.

    Normalised HERE, at the write chokepoint, rather than in each renderer: both
    renderers and `charter.md` inherit one rule, and a renderer added later
    cannot forget it. A row is one sentence by convention, so collapsing
    internal whitespace loses nothing real.
    """
    return " ".join(text.split())[:MAX_LEDGER_TEXT_CHARS]


async def ledger_add(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    text: str,
    source_ref: str | None = None,
    added_by: str = "foreground",
    evidence: str | None = None,
    source_quote: str | None = None,
    commit: bool = True,
) -> str:
    """Add an open ledger item and return its id.

    *commit=False* leaves the INSERT inside the caller's open transaction —
    for a caller that must make the insert atomic with its OWN bookkeeping
    write. The promotion path is why this exists: inserting the row and
    stamping ``promoted_item_id`` on the claiming shadow event must land
    together, because a crash between them leaves a ledger row no event
    claims — which the next sweep can duplicate once the row closes, and
    which the leak invariant reads as an unattributed write. Default True
    preserves every existing caller byte-for-byte.

    TWO PROVENANCE FIELDS, because two different writers answer two different
    questions and they must not share a column:

    *source_quote* is where the row CAME FROM — the extractor's verified
    transcript quote. Only the writer that created the row sets it, and no
    resolver overwrites it. The shadow report's live-mode leak invariant asks
    each extractor row exactly this, so it has to survive the row's whole life.

    *evidence* is how the row was RESOLVED — `repo_pulse_worker` replaces it
    with PR attribution when it absorbs an item. That is correct behaviour for
    a resolution field and fatal for a provenance one: sharing the column meant
    a promoted extractor row lost its quote the moment repo-pulse touched it,
    and then failed the invariant it had satisfied the day before.
    """
    if added_by not in VALID_ADDED_BY:
        raise ValueError(f"invalid added_by: {added_by!r}")
    text = _one_line(text)
    if not text:
        raise ValueError("ledger text must be non-empty")
    item_id = _new_id()
    await db.execute(
        """INSERT INTO session_ledger
           (id, session_id, text, status, source_ref, added_by, evidence,
            source_quote, created_at)
           VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
        (item_id, session_id, text, source_ref, added_by, evidence,
         source_quote, _now_iso()),
    )
    if commit:
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
        text = _one_line(text)
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


# One fetch of the ledger_all keyset walk. A module constant so tests can
# shrink it and genuinely cross page boundaries with a small corpus.
_LEDGER_ALL_PAGE = 10_000


async def ledger_all(
    db: aiosqlite.Connection,
    *,
    hard_cap: int = 200_000,
) -> list[dict]:
    """All ledger rows across sessions, oldest first (incl. added_by) — COMPLETE.

    Read seam for the shadow precision report and repo-pulse matching, both of
    which state facts about the WHOLE ledger — the leak invariant convicts on
    absence, so a silently truncated read turns "row not seen" into "row not
    written". Keyset-paginated internally (created_at, id) so no single fetch
    is unbounded; *hard_cap* is a resource tripwire that RAISES rather than
    truncates — a caller that needs a verdict refuses it instead of computing
    over a partial corpus. Sized from capacity, not history: 200k rows of this
    table is ~100MB in memory, far past any plausible real ledger (the live
    table holds thousands). Assumes a Row factory.
    """
    rows: list[dict] = []
    last: tuple[str, str] | None = None
    page = _LEDGER_ALL_PAGE
    while True:
        if last is None:
            cursor = await db.execute(
                "SELECT * FROM session_ledger ORDER BY created_at ASC, id ASC "
                "LIMIT ?",
                (page,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM session_ledger "
                "WHERE (created_at, id) > (?, ?) "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (*last, page),
            )
        batch = [dict(r) for r in await cursor.fetchall()]
        rows.extend(batch)
        if len(rows) > hard_cap:
            raise RuntimeError(
                f"ledger_all: session_ledger exceeds the {hard_cap}-row "
                "tripwire — refusing a partial read; raise hard_cap "
                "deliberately if the table is legitimately this large"
            )
        if len(batch) < page:
            return rows
        last = (batch[-1]["created_at"], batch[-1]["id"])


# Resource tripwire for ledger_stale_open, sized from capacity rather than
# history like ledger_all's: this subset (open/in_progress AND untouched past a
# threshold) is strictly smaller than the whole table, and 50k of them is far
# past any plausible ledger — the live table held 15 such rows on 2026-09-06.
# It RAISES rather than truncating, because the escalation sweep reports how
# many rows it DEFERRED, and a deferred count computed over a silently partial
# read is a wrong number that looks right.
_LEDGER_STALE_HARD_CAP = 50_000


async def ledger_stale_open(
    db: aiosqlite.Connection,
    *,
    untouched_before: str,
    added_by: frozenset[str] | set[str] | tuple[str, ...],
) -> list[dict]:
    """Unresolved ledger rows last touched before *untouched_before*, oldest first.

    "Unresolved" is open + in_progress: in_progress means someone started and
    never finished, which is exactly the state worth escalating, not an
    exemption from it. "Touched" is ``COALESCE(updated_at, created_at)``, so any
    ``ledger_update`` restarts the clock — a row someone is actively working
    never qualifies.

    *added_by* is a provenance allow-list (see
    ``session_awareness.ledger_escalation_config.escalate_added_by``). It is
    REQUIRED rather than defaulted: the caller deciding which provenance may
    create work for a human is a policy choice, and a default here would hide it.

    Ordered oldest-first so a caller applying a per-run cap takes the longest-
    undisposed rows rather than an arbitrary slice.
    """
    invalid = set(added_by) - VALID_ADDED_BY
    if invalid:
        raise ValueError(f"invalid added_by: {sorted(invalid)}")
    if not added_by:
        raise ValueError("added_by allow-list must be non-empty")
    placeholders = ", ".join("?" for _ in added_by)
    cursor = await db.execute(
        "SELECT * FROM session_ledger "
        "WHERE status IN ('open', 'in_progress') "
        f"AND added_by IN ({placeholders}) "  # noqa: S608 — placeholders only
        "AND COALESCE(updated_at, created_at) < ? "
        "ORDER BY COALESCE(updated_at, created_at) ASC, id ASC "
        "LIMIT ?",
        (*sorted(added_by), untouched_before, _LEDGER_STALE_HARD_CAP + 1),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    if len(rows) > _LEDGER_STALE_HARD_CAP:
        raise RuntimeError(
            f"ledger_stale_open: more than {_LEDGER_STALE_HARD_CAP} unresolved "
            "stale rows — refusing a partial read; raise the cap deliberately "
            "if the ledger is legitimately this large"
        )
    return rows


async def ledger_counts(db: aiosqlite.Connection, session_id: str) -> dict[str, int]:
    """Per-status row counts for a session's ledger (absent statuses omitted)."""
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM session_ledger WHERE session_id = ? GROUP BY status",
        (session_id,),
    )
    return {row[0]: row[1] for row in await cursor.fetchall()}
