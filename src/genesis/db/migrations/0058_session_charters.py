"""Add session_charters + session_ledger — the session-manager durable spine.

The charter-session model (PR-1, #1037): compaction implies continuity, so a
foreground session's origin (first typed prompt) is persisted at its first
compaction boundary and re-injected into every subsequent context window.
PR-2a moves that state from ``~/.genesis/sessions/<sid>/charter.json`` files
into canonical DB tables so the ledger MCP tools, the SessionStart injector,
the per-turn drift tag, and the dashboard all read/write one store with
field-scoped UPDATEs (no read-modify-write clobber between the PreCompact
hook and the MCP tools). ``charter.md`` under the session dir remains the
human-readable mirror.

Immutability contract: ``origin_prompt``/``origin_ts`` are write-once — the
column is nullable so a charter row can exist as a stub (created by an MCP
mission/ledger write before the session's first compaction), and every writer
fills origin only via ``WHERE origin_prompt IS NULL``. No writer ever
includes origin columns in a general UPDATE SET list.

``session_id`` here is the CC transcript session id (what the PreCompact hook
receives on stdin) — it matches ``cc_sessions.cc_session_id``, NOT
``cc_sessions.id``.

Additive + idempotent; canonical DDL mirrored in ``db/schema/_tables.py`` for
the fresh-DB path. Individual ``db.execute()`` calls (never ``executescript``)
so concurrent DDL from a subprocess writer stays serialized under WAL. No
commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_charters (
            session_id       TEXT PRIMARY KEY,  -- CC transcript session id (= cc_sessions.cc_session_id)
            transcript_path  TEXT,              -- last-seen transcript path (refreshed each compaction)
            origin_prompt    TEXT,              -- IMMUTABLE once non-NULL: first typed prompt, verbatim
            origin_ts        TEXT,              -- IMMUTABLE once written (paired with origin_prompt)
            mission          TEXT,              -- living; set via session_charter_update MCP tool
            pointers         TEXT NOT NULL DEFAULT '[]',  -- living; JSON array of strings
            compaction_count INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL,     -- ISO8601 UTC
            updated_at       TEXT               -- ISO8601 UTC
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_ledger (
            id          TEXT PRIMARY KEY,       -- uuid4 hex
            session_id  TEXT NOT NULL,          -- CC transcript session id (unenforced ref to session_charters)
            text        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','in_progress','done','absorbed','dropped')),
            source_ref  TEXT,                   -- where the item came from (plan file, transcript ref, ...)
            added_by    TEXT NOT NULL DEFAULT 'foreground'
                        CHECK(added_by IN ('foreground','ambient','pulse')),
            evidence    TEXT,                   -- PR-4 absorption refs (e.g. merged-PR link); NULL until then
            created_at  TEXT NOT NULL,
            updated_at  TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_ledger_session "
        "ON session_ledger(session_id, status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_charters_updated_at ON session_charters(updated_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS session_ledger")
    await db.execute("DROP TABLE IF EXISTS session_charters")
