"""Add attention_events — the passive-listening attention engine's SHADOW store.

Stores attention DECISIONS + REFERENCES + labels ONLY: activation/score/
triggers_fired/window_ref/clarity never carry ambient transcript text. The raw text
lives (and dies) in ambient.db on the edge; the offline shadow runner persists only
salience metadata + a window_ref (ids + ts range) here, so no cognition job that
scans this table can see conversation content. Feeds the shadow review + offline
calibration (design §6/§9). Additive + idempotent; the canonical DDL is mirrored in
db/schema/_tables.py for the fresh-DB path.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS attention_events (
            id                TEXT PRIMARY KEY,
            ts                TEXT NOT NULL,
            session_id        TEXT NOT NULL,
            activation        TEXT NOT NULL,
            score             REAL NOT NULL,
            triggers_fired    TEXT NOT NULL DEFAULT '[]',
            suppressors       TEXT NOT NULL DEFAULT '[]',
            window_ref        TEXT NOT NULL,
            mode_state        TEXT,
            clarity           REAL,
            l15_verdict       TEXT,
            acceptance_signal TEXT,
            snapshot_id       TEXT,
            config_version    TEXT,
            created_at        TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_attention_events_session ON attention_events(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_attention_events_ts ON attention_events(ts)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_attention_events_unlabeled "
        "ON attention_events(acceptance_signal) WHERE acceptance_signal IS NULL"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS attention_events")
