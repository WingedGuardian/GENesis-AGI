"""Create the ``capability_grants`` table — the WS-8 capability-grant matrix.

One row per (channel-domain, verb, risk-class) cell — e.g. ("email", "send",
"standard").  Autonomy is earned per channel-domain; this replaces the linear
L1–L7 ladder for ported domains (email first).

DARK on creation: nothing enforces or mutates cells at runtime yet.
Enforcement lives at the ``outreach_send`` chokepoint (PR-C); the
asymmetric / consequence-weighted competence upgrades and the staleness-decay
sweep land in PR-C/PR-D.  ``autonomy_state`` remains authoritative for every
existing autonomy reader until then — this migration adds a wholly new table
and touches nothing else.

Idempotent (``IF NOT EXISTS``).  Fresh installs get the same DDL via
``db/schema/_tables.py``; this migration covers existing installs.
"""

from __future__ import annotations

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS capability_grants (
        id            TEXT PRIMARY KEY,
        domain        TEXT NOT NULL,
        verb          TEXT NOT NULL,
        risk_class    TEXT NOT NULL DEFAULT 'standard' CHECK (risk_class IN (
            'standard', 'identity', 'bulk', 'financial'
        )),
        state         TEXT NOT NULL DEFAULT 'not_determined' CHECK (state IN (
            'not_determined', 'ask', 'granted', 'denied_permanent'
        )),
        successes     INTEGER NOT NULL DEFAULT 0,
        corrections   INTEGER NOT NULL DEFAULT 0,
        granted_at    TEXT,
        last_used_at  TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (domain, verb, risk_class)
    )
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_capability_grants_domain "
    "ON capability_grants(domain, state)",
)


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (and its indexes) — development/testing only."""
    await db.execute("DROP TABLE IF EXISTS capability_grants")
