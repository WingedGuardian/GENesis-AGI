"""CRUD for attention_events — the passive-listening attention engine's SHADOW store.

Rows are attention DECISIONS + REFERENCES + labels ONLY (activation/score/
triggers_fired/window_ref/clarity); NEVER ambient transcript text (firewall). The
offline shadow runner is the only writer; a future dashboard tab (PR2) reads for the
should/shouldn't review + offline calibration.
"""
from __future__ import annotations

import aiosqlite

# Column order — the offline runner's row tuples (ShadowStoreConsumer._to_row) MUST match.
COLUMNS = (
    "id", "ts", "session_id", "activation", "score", "triggers_fired", "suppressors",
    "window_ref", "mode_state", "clarity", "l15_verdict", "acceptance_signal",
    "snapshot_id", "config_version", "created_at",
)


async def bulk_upsert_events(db: aiosqlite.Connection, rows: list[tuple]) -> int:
    """Idempotent bulk INSERT OR REPLACE of shadow AttentionEvents. ``rows`` are tuples
    in ``COLUMNS`` order. One transaction (executemany + commit); returns the count."""
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(COLUMNS))
    await db.executemany(
        f"INSERT OR REPLACE INTO attention_events ({', '.join(COLUMNS)}) "  # noqa: S608
        f"VALUES ({placeholders})",
        rows,
    )
    await db.commit()
    return len(rows)


async def count(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) AS n FROM attention_events")
    row = await cursor.fetchone()
    return row["n"] if row else 0
