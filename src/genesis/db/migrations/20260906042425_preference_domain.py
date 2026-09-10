"""Add ``preference_domain`` to ``memory_metadata`` (MW-4 satellite).

A preference is not a scalar fact: "favorite color" legitimately differs by
domain (work vs vehicles vs this-month). MW-1 already classifies
``speech_act='preference'`` at extraction; this column captures the DOMAIN
qualifier the extractor now emits alongside it, so a detected conflict can
dissolve into two coexisting domain-scoped statements instead of newest-wins.
Open vocabulary by design (domains are an open set — "work", "vehicles",
"food"); normalized lowercase at the write path (memory/judgment.py).

WRITE-ONLY like the other judgment axes: nothing reads it yet; the consumer is
MW-4 recall ranking (follow-up b51542de). # GROUNDWORK(mw-4-preference-domain)

NULLable, no default, no backfill — NULL means "captured before this column
existed or not a preference", never a judgment.

Self-contained + idempotent: the ADD is PRAGMA-guarded (applies via the base
path OR the numbered runner; whichever ran first, the guard skips). No commit —
the runner owns the transaction. Mirrored in ``_migrate_add_columns``
(schema_both_build_paths).
"""

from __future__ import annotations

import aiosqlite


async def _add_column(db: aiosqlite.Connection, table: str, col: str, decl: str) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if await cursor.fetchone() is None:
        return  # fresh install: create_all_tables builds the full canonical shape
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def up(db: aiosqlite.Connection) -> None:
    await _add_column(db, "memory_metadata", "preference_domain", "TEXT")


async def down(db: aiosqlite.Connection) -> None:
    """No-op: a NULLable write-only column is harmless to leave in place.

    (0081 does DROP its columns; the difference is a choice, not a fleet
    constraint — dropping buys nothing for a column nothing reads.)"""
