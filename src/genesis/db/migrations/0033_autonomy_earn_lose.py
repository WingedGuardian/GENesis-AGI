"""WS-8 PR-D — the earn/lose autonomy loop + auto-revert (schema).

Three coupled schema changes that let an email capability cell be *earned*
(promoted to GRANTED with the owner's approval) and *lost* (auto-reverted to
ASK on any confirmed harm signal):

1. ``capability_grants.weighted_corrections`` — Σ of consequence-weighted
   corrections. Demotion (GRANTED→ASK) is deterministic on any correction; this
   accumulator governs how hard the cell is to RE-earn (heavier harm ⇒ deeper
   crater ⇒ more clean successes before re-promotion).
2. ``capability_grants.last_decayed_at`` — bookkeeping for the staleness-decay
   sweep (a long-unused GRANTED cell decays GRANTED→NOT_DETERMINED).
3. ``autonomous_email_sends`` — the autonomous-send ledger. One row per email
   the gate let through under a GRANTED cell (NOT owner-approved holds). It is
   the keystone the owner-visibility "Activity" tab, the flag-as-bad correction,
   and the per-cell rate-limit guard all read, because ``outreach_history``
   carries no recipient/thread/cell column.

Plus the **seed neutralization** (WS-8 all-ASK decision): the PR-C Option-B seed
(migration 0032) pre-granted ``email:send:standard``. Promotion must be earned +
owner-approved, never automatic, so this migration removes that pristine grant —
surgically, so it never touches a cell the owner has since acted on.

Idempotent (PRAGMA-guarded ALTERs, ``IF NOT EXISTS`` table/indexes, a WHERE-
scoped DELETE). Fresh installs get the columns + table from ``db/schema/_tables``;
this migration covers existing installs. ``up()`` must NOT call
``db.commit()``/``BEGIN`` — the runner owns the transaction.
"""

from __future__ import annotations

import contextlib

import aiosqlite

_AUTONOMOUS_SENDS_DDL = """
    CREATE TABLE IF NOT EXISTS autonomous_email_sends (
        id                  TEXT PRIMARY KEY,
        outreach_id         TEXT,
        thread_id           TEXT,
        recipient           TEXT NOT NULL,
        subject             TEXT,
        cell_domain         TEXT NOT NULL,
        cell_verb           TEXT NOT NULL,
        cell_risk_class     TEXT NOT NULL,
        sent_at             TEXT NOT NULL,
        flagged_at          TEXT,
        created_at          TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_AUTONOMOUS_SENDS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_autonomous_email_sends_cell "
    "ON autonomous_email_sends(cell_domain, cell_verb, cell_risk_class, sent_at)",
    "CREATE INDEX IF NOT EXISTS idx_autonomous_email_sends_sent "
    "ON autonomous_email_sends(sent_at)",
)

#: The pristine PR-C Option-B seed, scoped so it only matches an untouched grant.
_REVERT_SEED = (
    "DELETE FROM capability_grants "
    "WHERE id = 'email:send:standard' AND state = 'granted' "
    "AND successes = 0 AND corrections = 0"
)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    cols = await _columns(db, "capability_grants")
    if "weighted_corrections" not in cols:
        await db.execute(
            "ALTER TABLE capability_grants "
            "ADD COLUMN weighted_corrections REAL NOT NULL DEFAULT 0.0"
        )
    if "last_decayed_at" not in cols:
        await db.execute(
            "ALTER TABLE capability_grants ADD COLUMN last_decayed_at TEXT"
        )

    await db.execute(_AUTONOMOUS_SENDS_DDL)
    for stmt in _AUTONOMOUS_SENDS_INDEXES:
        await db.execute(stmt)

    # Seed neutralization (all-ASK): remove the pristine PR-C Option-B grant so
    # nothing sends autonomously until earned + owner-approved.
    await db.execute(_REVERT_SEED)


async def down(db: aiosqlite.Connection) -> None:
    """Reverse the schema changes — development/testing only.

    Restores the PR-C Option-B seed for symmetry. SQLite ``DROP COLUMN`` needs
    3.35+; suppressed if unavailable (the columns are harmless if they remain).
    """
    await db.execute("DROP TABLE IF EXISTS autonomous_email_sends")
    for col in ("weighted_corrections", "last_decayed_at"):
        with contextlib.suppress(aiosqlite.OperationalError):
            await db.execute(f"ALTER TABLE capability_grants DROP COLUMN {col}")
    await db.execute(
        "INSERT OR IGNORE INTO capability_grants "
        "(id, domain, verb, risk_class, state, granted_at, created_at, updated_at) "
        "VALUES ('email:send:standard', 'email', 'send', 'standard', 'granted', "
        "datetime('now'), datetime('now'), datetime('now'))"
    )
