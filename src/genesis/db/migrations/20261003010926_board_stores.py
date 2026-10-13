"""The work board's own stores: card pointers, open questions, and its event log.

WHY THESE TABLES EXIST. The work board lives on GitHub Projects v2 — cards,
columns, positions and dependencies are GitHub's facts, and Genesis mirrors
none of them. Genesis keeps exactly three things GitHub cannot hold:

* ``board_links`` — which private Genesis record (a session-ledger row or a
  follow-up) a public issue was PROMOTED from, with the promotion's audit: who
  approved it, the privacy-scan receipt, and the hash of the exact body that
  was scanned. Written only after the issue exists, so a row is a fact, never
  an intention.
* ``open_questions`` + ``open_question_blocks`` — unresolved forks that must
  never reach GitHub, and the work each one blocks. A block is an edge, not a
  list entry: promotion refuses a record that an unverified question blocks,
  and a card shows the block as an advisory afterwards.
* ``board_events`` — the append-only log (the spec's "dispatch/ask log"):
  every move Genesis observes or makes, every promotion, every override. The
  board's metrics and its own-write attribution are derived from it, so no
  status is ever mirrored into a table of its own.

WHY NOT AN EXISTING STORE (the New-Store Gate, recorded where the store is
born). ``follow_ups`` readers (ego dispatch via ``get_actionable``, the morning
report via ``get_pending``) would surface questions and pointers as actionable
work; ``session_ledger`` is per-session and re-injected into every window, so a
long-lived question there would be noise in every compaction; and
``pending_issue_posts`` (the contributor drain's hold store) is a queue whose
terminal rows are pruned after 30 days, while a promotion pointer must outlive
the issue it names. Consistency: one writer per table — promotion writes
``board_links``, the open-question tools write the question tables, the board
reconciler and promotion append ``board_events``. Retention: terminal questions
(resolved / dropped) and their blocks after 90 days, events after 180 days,
both by ``scripts/prune_board.py`` on the disk-hygiene timer; pointers are never
pruned. Backup: rides genesis.db.

``board_events.event`` deliberately carries NO CHECK constraint: SQLite cannot
ALTER a CHECK, and the vocabulary grows when dispatch lands. The closed set is
enforced in ``db/crud/board.py`` instead (the ``pr_verifications`` verdict
precedent). The partial UNIQUE index on ``(event, observed_change_key)`` is the
dedup for an observed GitHub change: a reconcile tick that re-reads the same
status change is absorbed by the schema, not by a convention at the call site.

Additive and idempotent — CREATE ... IF NOT EXISTS, no rebuild. Fresh installs
get the identical DDL from ``schema/_tables.py``; ``tests/test_db/test_board_crud.py``
pins the two build paths in parity.
"""

from __future__ import annotations

import aiosqlite

TABLES = {
    "board_links": """
    CREATE TABLE IF NOT EXISTS board_links (
        id               TEXT PRIMARY KEY,
        source_kind      TEXT NOT NULL CHECK (source_kind IN ('ledger','follow_up')),
        source_id        TEXT NOT NULL,
        repo             TEXT NOT NULL,
        issue_number     INTEGER NOT NULL,
        project_item_id  TEXT,
        adopted          INTEGER NOT NULL DEFAULT 0 CHECK (adopted IN (0,1)),
        promoted_by      TEXT NOT NULL,
        approval_id      TEXT,
        scan_receipt     TEXT NOT NULL,
        body_sha256      TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        UNIQUE(source_kind, source_id),
        UNIQUE(repo, issue_number)
    )
    """,
    "open_questions": """
    CREATE TABLE IF NOT EXISTS open_questions (
        id          TEXT PRIMARY KEY,
        question    TEXT NOT NULL,
        context     TEXT,
        status      TEXT NOT NULL DEFAULT 'unverified'
                      CHECK (status IN ('unverified','resolved','dropped')),
        resolution  TEXT,
        raised_by   TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        closed_at   TEXT
    )
    """,
    "open_question_blocks": """
    CREATE TABLE IF NOT EXISTS open_question_blocks (
        question_id  TEXT NOT NULL,
        target_kind  TEXT NOT NULL CHECK (target_kind IN ('ledger','follow_up','card')),
        target_id    TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        PRIMARY KEY (question_id, target_kind, target_id)
    )
    """,
    "board_events": """
    CREATE TABLE IF NOT EXISTS board_events (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        event                TEXT NOT NULL,
        repo                 TEXT,
        issue_number         INTEGER,
        project_item_id      TEXT,
        attempt              INTEGER,
        worker               TEXT,
        reason               TEXT,
        observed_change_key  TEXT,
        detail               TEXT,
        created_at           TEXT NOT NULL
    )
    """,
}

INDEXES = (
    # board_links needs none: its two UNIQUE constraints already build the
    # (source_kind, source_id) and (repo, issue_number) lookup indexes.
    "CREATE INDEX IF NOT EXISTS idx_open_questions_status ON open_questions(status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_oq_blocks_target "
    "ON open_question_blocks(target_kind, target_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_board_events_change "
    "ON board_events(event, observed_change_key) WHERE observed_change_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_board_events_issue "
    "ON board_events(repo, issue_number, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_board_events_created ON board_events(created_at)",
)


async def up(db: aiosqlite.Connection) -> None:
    for ddl in TABLES.values():
        await db.execute(ddl)
    for ddl in INDEXES:
        await db.execute(ddl)
