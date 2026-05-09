"""Add J-9 eval infrastructure tables.

Supports the cognitive architecture paper's 5 eval dimensions:
1. Memory retrieval quality (precision@K, MRR, hit rate)
2. System improvement over time (composite metric)
3. Ego proposal quality trajectory
4. Cognitive loop value (recall vs no-recall)
5. Procedural learning effectiveness

Two tables:
- eval_events: append-only event log for all measurable eval signals
- eval_snapshots: periodic (daily/weekly) aggregated metrics
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS eval_events (
            id          TEXT PRIMARY KEY,
            timestamp   TEXT NOT NULL,
            dimension   TEXT NOT NULL
                        CHECK (dimension IN (
                            'memory', 'ego', 'procedure', 'cognitive', 'system'
                        )),
            event_type  TEXT NOT NULL,
            subject_id  TEXT,
            session_id  TEXT,
            metrics_json TEXT NOT NULL,
            created_at  TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_events_dimension "
        "ON eval_events(dimension, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_events_type "
        "ON eval_events(event_type, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_events_session "
        "ON eval_events(session_id)"
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS eval_snapshots (
            id           TEXT PRIMARY KEY,
            period_start TEXT NOT NULL,
            period_end   TEXT NOT NULL,
            period_type  TEXT NOT NULL
                         CHECK (period_type IN ('daily', 'weekly')),
            dimension    TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            created_at   TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_snapshots_period "
        "ON eval_snapshots(dimension, period_end)"
    )

    # Runner manages the transaction — do not call db.commit() here.
