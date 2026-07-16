"""Add repo_pulse_runs + repo_pulse_annotations — the repo-pulse annotator store.

Session-manager PR-4a: a detached worker (spawned at SessionStart boundaries)
enumerates merged PRs since a global cursor and matches them against OPEN
``session_ledger`` rows across ALL sessions. Two tiers:

- **exact** — the PR body/title carries an explicit ``Ledger: <32-hex>``
  marker naming an open row: auto-absorbed live (``session_ledger`` UPDATE
  with PR evidence) and recorded here as ``status='applied'``. A bare 32-hex
  id WITHOUT the marker is only ever ``status='proposed'`` (a PR can cite a
  row as context without completing it).
- **fuzzy** — a headless Haiku judge scores open-item ↔ PR-title/body
  matches: proposals ONLY (surfaced at charter injection), never a ledger
  write, in every mode. Proposal resolutions (confirmed/rejected) ARE the
  precision measurement for this tier.

Two tables for the same reason as the PR-3 shadow store: a *run* row exists
even at zero matches (no_new_prs runs prove the cursor advanced honestly;
failed runs must not advance it), and *annotation* rows carry the match
evidence for adjudication. ``UNIQUE(tier, item_id, pr_number)`` dedupes
re-covered enumeration windows (INSERT OR IGNORE semantics in CRUD).

``item_session_id`` snapshots the ledger row's session so injection can
filter per-session without joining live tables. Cursor state itself lives in
``~/.genesis/repo_pulse/cursor.json`` (worker-owned), not here. Retention:
45 days via ``crud.repo_pulse.prune_repo_pulse`` (disk_hygiene.sh).

Additive + idempotent; DDL mirrored in ``db/schema/_tables.py``. Individual
``db.execute()`` calls, no commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_pulse_runs (
            run_id         TEXT PRIMARY KEY,    -- uuid4 hex
            started_at     TEXT NOT NULL,       -- ISO8601 UTC
            finished_at    TEXT,
            trigger        TEXT NOT NULL,       -- 'session_start' | 'manual'
            repo           TEXT,                -- live-resolved owner/name slug
            cursor_before  TEXT,                -- ISO mergedAt watermark at run start
            cursor_after   TEXT,                -- watermark after (== before unless advanced)
            status         TEXT NOT NULL
                           CHECK(status IN ('ok','failed','timeout','lock_busy','no_new_prs')),
            n_prs          INTEGER NOT NULL DEFAULT 0,
            n_open_items   INTEGER NOT NULL DEFAULT 0,
            n_exact        INTEGER NOT NULL DEFAULT 0,
            n_fuzzy        INTEGER NOT NULL DEFAULT 0,
            latency_ms     INTEGER,
            prompt_version TEXT,
            model          TEXT,
            mode           TEXT NOT NULL DEFAULT 'live',  -- settings posture the run ran under
            detail         TEXT                -- freeform: failure reason / 'limit_hit' / notes
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_pulse_annotations (
            id              TEXT PRIMARY KEY,   -- uuid4 hex
            run_id          TEXT NOT NULL,      -- unenforced ref to _runs
            observed_at     TEXT NOT NULL,      -- ISO8601 UTC
            tier            TEXT NOT NULL CHECK(tier IN ('exact','fuzzy')),
            item_id         TEXT NOT NULL,      -- session_ledger.id
            item_session_id TEXT,               -- ledger row's session (injection filter)
            item_text       TEXT,               -- snapshot at observation time
            pr_number       INTEGER NOT NULL,
            pr_title        TEXT,
            pr_merged_at    TEXT,
            confidence      REAL,               -- fuzzy-judge score (NULL on exact tier)
            rationale       TEXT,               -- judge reason / 'ledger-marker' / 'bare-hex'
            status          TEXT NOT NULL
                            CHECK(status IN ('applied','proposed','confirmed',
                                             'rejected','superseded')),
            resolved_at     TEXT,
            resolution_ref  TEXT                -- what resolved it (evidence text / actor)
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rpa_dedupe "
        "ON repo_pulse_annotations(tier, item_id, pr_number)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rpa_status ON repo_pulse_annotations(status, observed_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rpa_session "
        "ON repo_pulse_annotations(item_session_id, status)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_rpr_started ON repo_pulse_runs(started_at)")


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS repo_pulse_annotations")
    await db.execute("DROP TABLE IF EXISTS repo_pulse_runs")
