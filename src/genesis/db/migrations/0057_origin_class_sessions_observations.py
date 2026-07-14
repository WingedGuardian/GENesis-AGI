"""Add ``origin_class`` to ``cc_sessions`` and ``observations`` (WS-3 B4 PR-2).

The gate-2 (identity) L-tier substrate: session origin today lives only in a
transient env var on dispatched CC children (``GENESIS_SESSION_ORIGIN``), so
nothing server-side can answer "was that session external?" after the fact —
which is why the gate-2 emit has been a hardcoded-first_party NO-OP. This
migration gives sessions a durable origin column (stamped at registration
from the dispatch profile — NEVER a tool scan) and observations an
``origin_class`` so reflection-derived ``user_model_delta`` rows can carry a
run-level provenance aggregate.

Nullable, no backfill: a historical NULL means "unknown". Shadow aggregation
treats NULL as first_party (pre-substrate rows must not manufacture external
signal); any future ENFORCE consumer must apply the WS-3 fail-closed
normalization at gate time instead (``security.immunity.effective_origin_class``).

Fresh/test DBs get the columns from the canonical CREATE TABLEs in
``db/schema/_tables.py``; this numbered migration covers the existing-DB
upgrade path. Idempotent: PRAGMA-guarded ADD COLUMN, O(1) (nullable, no
default). No commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def _add_column(db: aiosqlite.Connection, table: str) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if await cursor.fetchone():
        col_cursor = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608 -- table name from the fixed call sites below
        cols = {row[1] for row in await col_cursor.fetchall()}
        if "origin_class" not in cols:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN origin_class TEXT")  # noqa: S608


async def up(db: aiosqlite.Connection) -> None:
    await _add_column(db, "cc_sessions")
    await _add_column(db, "observations")
