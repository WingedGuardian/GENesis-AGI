"""Create the ``outcome_events`` table — the Self-Improvement Outcome Bus ledger.

An append-only, attributed, quality-weighted record of what actually happened
after Genesis acted: post-execution outcomes (T1 ground truth), user decisions
(T2/T3), and coverage events. It unifies signals that today are siloed or buried
(e.g. the T1 "did it work" outcome is currently a ``|completed:``/``|failed:``
suffix on ``ego_proposals.user_response`` — captured for ~1.6% of executions).

Neutral framing (``outcome_events``, not ``reward_signals``): this is observation,
not RL training. The bus is DARK on creation — nothing emits or consumes here;
Step 1 only lays the table + CRUD.

Idempotent (``IF NOT EXISTS``). Fresh installs get the same DDL via
``db/schema/_tables.py``; this migration covers existing installs.

The UNIQUE key ``(source, ref_type, ref_id, signal_type)`` makes harvest/backfill
idempotent. ``signal_type`` uses a controlled vocabulary so distinct event classes
on the SAME ref (e.g. a T2 ``user_decision`` and a later T1 ``execution_outcome``
on one proposal) never collide on the key — preventing the bus from silently
dropping the higher-tier ground-truth signal.
"""

from __future__ import annotations

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS outcome_events (
        id                TEXT PRIMARY KEY,
        source            TEXT NOT NULL,
        ref_type          TEXT NOT NULL,
        ref_id            TEXT NOT NULL,
        domain            TEXT,
        signal_type       TEXT NOT NULL,
        signal_class      TEXT NOT NULL DEFAULT 'implicit'
                              CHECK (signal_class IN ('implicit', 'explicit')),
        signal_tier       INTEGER NOT NULL CHECK (signal_tier IN (1, 2, 3)),
        polarity          TEXT CHECK (polarity IN ('positive', 'negative', 'neutral')),
        value             REAL,
        stated_confidence REAL,
        prediction_error  REAL,
        reason            TEXT,
        reason_text       TEXT,
        metadata          TEXT,
        harvested_from    TEXT,
        occurred_at       TEXT NOT NULL,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (source, ref_type, ref_id, signal_type)
    )
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_domain ON outcome_events(domain)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_source ON outcome_events(source)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_tier ON outcome_events(signal_tier)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_signal_type ON outcome_events(signal_type)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_ref ON outcome_events(ref_type, ref_id)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_occurred ON outcome_events(occurred_at)",
    # Calibration reads pull tier-1 rows that carry both a stated confidence and
    # a graded outcome value; partial index keeps that scan cheap.
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_calibration "
    "ON outcome_events(domain, signal_tier) "
    "WHERE stated_confidence IS NOT NULL AND value IS NOT NULL",
)


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (and its indexes) — development/testing only."""
    await db.execute("DROP TABLE IF EXISTS outcome_events")
