"""Add owed rechecks to closed verification rows (issue #3694).

The nullable TEXT column stores trigger/recheck/detail with empty results and
null done_at. Backfill only typed event_gated gaps with all three text fields;
legacy prose is never interpreted. Invalid evidence is skipped without aborting
upgrade. Existing rechecks/results are never replaced on rerun.

No CHECK or destructive down migration. Column order matches canonical DDL;
the runner owns commit and rollback. Fresh installs may already have the column.
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


#: A closed row's evidence, or an empty object when it is not valid JSON.
_DOC = "CASE WHEN json_valid(evidence) THEN evidence ELSE '{}' END"

#: The event-gated gaps of that document that carry all three fields as text.
_EVENT_GAPS = (
    f"FROM json_each({_DOC}, '$.gaps') AS g "
    f"WHERE json_extract({_DOC}, g.fullkey || '.kind') = 'event_gated' "
    f"AND json_type({_DOC}, g.fullkey || '.trigger') = 'text' "
    f"AND json_type({_DOC}, g.fullkey || '.recheck') = 'text' "
    f"AND json_type({_DOC}, g.fullkey || '.detail') = 'text'"
)


async def up(db: aiosqlite.Connection) -> None:
    await _add_column(db, "pr_verifications", "rechecks", "TEXT")
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pr_verifications'"
    )
    if await cursor.fetchone() is None:
        return
    await db.execute(
        "UPDATE pr_verifications SET rechecks = (SELECT json_group_array(json_object("
        f"'trigger', json_extract({_DOC}, g.fullkey || '.trigger'), "
        f"'recheck', json_extract({_DOC}, g.fullkey || '.recheck'), "
        f"'detail', json_extract({_DOC}, g.fullkey || '.detail'), "
        f"'results', json('[]'), 'done_at', NULL)) {_EVENT_GAPS}) "
        f"WHERE status = 'closed' AND rechecks IS NULL "
        f"AND json_type({_DOC}, '$.gaps') = 'array' AND EXISTS (SELECT 1 {_EVENT_GAPS})"
    )


async def down(db: aiosqlite.Connection) -> None:
    """No-op, deliberately: the column holds owed measurements and their results,
    written by sessions that have since ended, and DROP COLUMN would purge them."""
