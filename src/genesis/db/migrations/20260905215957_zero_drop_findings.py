"""Zero-drop findings: one row per standing stranded-work condition.

The zero-drop mandate is that "what work has fallen through the cracks?"
is answered by a reconciler that ENUMERATES, never by a session that
remembers — and an enumeration is only trustworthy if the same condition
seen twice is the same row twice. That identity is what this table holds.

Identity is ``(class, branch)``, deliberately NOT the tip SHA: a finding
keyed on the SHA would become a new row on every commit, resetting the
consecutive-run counter and silently disarming the escalation the amended
§8.11 requires. The SHA is carried as EVIDENCE (``tip_sha``) and as the
expiry key for an acknowledgement (``acked_tip_sha``) — an ack means "I
looked at this branch AS IT WAS", so it expires the moment the branch
moves.

Why a new table rather than an existing store (New-Store Gate — the full
justification, with each rejected candidate, lives in the module docstring
of ``db/crud/zero_drop.py``, beside the code that depends on it):
``observations`` drops duplicate findings instead of counting them and has
no acknowledgement concept; ``alert_events`` holds exactly one open row per
alert id; ``reflex_signals`` is the closest SHAPE (and this schema adapts
it) but owns the self-bug-repair lifecycle; ``repo_pulse_annotations`` is
per-match-EVENT grain, not per-standing-condition.

Additive and idempotent in ``up()``; ``down()`` is destructive by design
(dev/test affordance, not a production rollback path). DDL is
byte-identical to the ``db/schema/_tables.py`` mirror per
schema_both_build_paths. The runner owns the transaction — no commit here.
"""

from __future__ import annotations

import aiosqlite

_TABLE = "zero_drop_findings"

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS zero_drop_findings (
        id                TEXT PRIMARY KEY,
        class             TEXT NOT NULL
                            CHECK (class IN (
                              'unpushed_branch','pushed_no_pr','dirty_worktree')),
        branch            TEXT NOT NULL,
        tip_sha           TEXT,
        ahead_count       INTEGER,
        worktree_path     TEXT,
        status            TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open','acked','resolved')),
        first_seen_at     TEXT NOT NULL,
        last_seen_at      TEXT NOT NULL,
        consecutive_runs  INTEGER NOT NULL DEFAULT 1,
        escalated_at      TEXT,
        last_run_id       TEXT,
        ack_reason        TEXT,
        acked_at          TEXT,
        acked_tip_sha     TEXT,
        resolved_at       TEXT,
        reopen_count      INTEGER NOT NULL DEFAULT 0,
        details           TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        UNIQUE(class, branch)
    )
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_zero_drop_findings_status ON zero_drop_findings(status)",
    "CREATE INDEX IF NOT EXISTS idx_zero_drop_findings_last_seen "
    "ON zero_drop_findings(last_seen_at)",
)


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    if await _table_exists(db, _TABLE):
        await db.execute(f"DROP TABLE {_TABLE}")  # noqa: S608 — literal, ours
