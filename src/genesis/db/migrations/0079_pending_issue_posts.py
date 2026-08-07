"""Create ``pending_issue_posts`` — the Contributor Work-Log hold store.

A local curator campaign drafts a public GitHub issue (from the follow-up
backlog or a codebase scan). The ``contributor_issue_propose`` MCP tool
sanitizes it server-side (``contribution.sanitize.scan_prose``) and, if clean,
records the fully-resolved draft here (status='held') plus a linked
``approval_requests`` row. A periodic resolution watcher
(``contributor_issue_watcher``) then posts the issue below the gate on owner
approval, or expires the hold on rejection/timeout — the same shape as the WS-8
email autonomy gate (``pending_email_sends`` + ``email_gate_watcher``).

``request_id`` is UNIQUE — the schema-level double-post guard: even if the
watcher fires twice, only one row per approval can transition out of 'held'.

Idempotent (``IF NOT EXISTS``). Fresh installs get the same DDL via
``db/schema/_tables.py``; this migration covers existing installs.
"""

from __future__ import annotations

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS pending_issue_posts (
        id                  TEXT PRIMARY KEY,
        request_id          TEXT NOT NULL UNIQUE,   -- FK approval_requests.id; double-post guard
        repo                TEXT NOT NULL,          -- target repo, e.g. owner/name
        title               TEXT NOT NULL,          -- sanitized issue title
        body                TEXT NOT NULL,          -- sanitized issue body
        labels              TEXT,                   -- JSON array of label names
        source              TEXT NOT NULL,          -- 'follow_up' | 'codebase'
        source_ref          TEXT,                   -- follow_up id (close-loop link), nullable
        cell_domain         TEXT NOT NULL,          -- capability cell (shadow-gate): 'github'
        cell_verb           TEXT NOT NULL,          -- 'issue_create'
        cell_risk_class     TEXT NOT NULL,          -- RiskClass, e.g. 'bulk'
        held_at             TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'held'
                                CHECK (status IN ('held', 'posted', 'rejected', 'expired', 'dry_run')),
        issue_number        INTEGER,                -- captured after a successful post
        issue_url           TEXT,
        posted_at           TEXT,
        rejected_at         TEXT,
        dry_run_at          TEXT                    -- propose_only: approved but not posted (terminal)
    )
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_pending_issue_posts_status ON pending_issue_posts(status)",
)


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (and its indexes) — development/testing only."""
    await db.execute("DROP TABLE IF EXISTS pending_issue_posts")
