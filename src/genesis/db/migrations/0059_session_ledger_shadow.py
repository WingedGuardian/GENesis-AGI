"""Add session_ledger_shadow_runs + _events — the ambient extractor SHADOW store.

Session-manager PR-3: a detached worker (spawned at each PreCompact boundary)
extracts missed agreements/pivots from the transcript delta via headless Haiku
and logs PROPOSALS here — never to the live ``session_ledger`` — until
agreement-detection precision vs foreground ``session_ledger_add`` rows is
proven and the user flips the mode (a later PR).

Two tables because the precision math needs both sides: a *run* row exists
even when zero proposals emerge (a successful empty run charges its window's
missed foreground rows as false negatives; a FAILED run must not), and *event*
rows carry the full proposal text (the FP-adjudication protocol reviews the
verbatim candidate — capability_shadow's content-carrying posture, deliberately
unlike immunity_shadow's no-content rule).

``session_id`` is the CC transcript session id (= cc_sessions.cc_session_id),
matching session_charters/session_ledger. Retention: 45 days via
``crud.session_ledger_shadow.prune_session_ledger_shadow`` (disk_hygiene.sh).

Additive + idempotent; DDL mirrored in ``db/schema/_tables.py``. Individual
``db.execute()`` calls, no commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_ledger_shadow_runs (
            run_id         TEXT PRIMARY KEY,    -- uuid4 hex
            session_id     TEXT NOT NULL,       -- CC transcript session id
            started_at     TEXT NOT NULL,       -- ISO8601 UTC
            finished_at    TEXT,
            start_byte     INTEGER NOT NULL,
            end_byte       INTEGER NOT NULL,
            trigger        TEXT NOT NULL,       -- precompact trigger (manual/auto) or 'backfill'
            status         TEXT NOT NULL
                           CHECK(status IN ('ok','failed','timeout','lock_busy','empty_delta')),
            truncated      INTEGER NOT NULL DEFAULT 0,  -- delta exceeded the prompt budget
            n_user_turns   INTEGER NOT NULL DEFAULT 0,
            n_proposals    INTEGER NOT NULL DEFAULT 0,
            latency_ms     INTEGER,
            prompt_version TEXT,
            model          TEXT,
            mode           TEXT NOT NULL DEFAULT 'shadow',  -- posture the run was taken under
            detail         TEXT                -- freeform failure reason / notes
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_ledger_shadow_events (
            id              TEXT PRIMARY KEY,   -- uuid4 hex
            run_id          TEXT NOT NULL,      -- unenforced ref to _runs
            observed_at     TEXT NOT NULL,      -- ISO8601 UTC
            session_id      TEXT NOT NULL,
            kind            TEXT NOT NULL CHECK(kind IN ('agreement','pivot')),
            text            TEXT NOT NULL,      -- full proposal (<= live ledger text cap)
            turn_ref        TEXT,               -- transcript entry uuid (fallback ts)
            quote_preview   TEXT,               -- first 200ch of the model's verbatim quote
            quote_hash      TEXT,               -- sha256 of the full quote
            quote_verified  INTEGER NOT NULL DEFAULT 0,  -- quote is a substring of the turn text
            match_kind      TEXT NOT NULL DEFAULT 'none'
                            CHECK(match_kind IN ('exact','fuzzy','none')),
            matched_item_id TEXT,               -- session_ledger.id when matched
            match_score     REAL,
            duplicate_of    TEXT,               -- prior shadow event id (crash-recovery re-covers)
            mode            TEXT NOT NULL DEFAULT 'shadow'
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_slsr_session "
        "ON session_ledger_shadow_runs(session_id, started_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_slsr_started ON session_ledger_shadow_runs(started_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_slse_session "
        "ON session_ledger_shadow_events(session_id, observed_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_slse_observed ON session_ledger_shadow_events(observed_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS session_ledger_shadow_events")
    await db.execute("DROP TABLE IF EXISTS session_ledger_shadow_runs")
