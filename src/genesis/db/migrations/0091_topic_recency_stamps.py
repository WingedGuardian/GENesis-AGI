"""Two timestamps so the peer line can pick the more RECENT topic honestly.

The concurrent-session peer line chooses between two accounts of what a session
is doing: the summary the memory-extraction job writes, and the session's own
charter mission. Choosing by recency needs a timestamp for EACH — and neither
table had one that means what the comparison needs.

  - ``session_charters.mission_updated_at`` — ``updated_at`` cannot answer "when
    was the MISSION set": ``set_pointers`` and the charter upsert bump it too,
    so a pointer edit would make a stale founding mission look freshly declared.

  - ``cc_sessions.topic_updated_at`` — symmetric, and the reason this migration
    carries both. ``last_extracted_at`` is a PASS watermark, not the topic's
    age: ``update_extraction_watermark`` and ``update_topic_and_keywords`` are
    different functions, and the extraction job advances the watermark
    unconditionally while writing the topic only when it has one. MEASURED on a
    live install: 219 of 899 rows carry a watermark with no topic at all. Using
    the watermark as the topic's age would commit, on the other side of the
    comparison, exactly the defect ``mission_updated_at`` exists to avoid — a
    pass that produced no new topic would refresh the timestamp and suppress a
    genuinely newer mission.

Both are NULLABLE with NO default and NO backfill, deliberately. For rows
written before this migration we do not know when either value was set, and
inventing a timestamp would be worse than admitting that: an invented-recent
stamp on either side would silently reorder the peer line. The consumer treats
NULL as "cannot compare" and keeps preferring the extracted summary, which is
the behaviour before this change — so the migration is inert on arrival and can
only start deciding once a real write stamps a real time.

Additive and idempotent in ``up()``; ``down()`` is destructive by design (it
drops the columns and every stamp with them) and, per house convention, is a
dev/test affordance rather than a production rollback path. Both ALTERs are
PRAGMA/duplicate-guarded. Mirrored in ``db/schema/_tables.py`` (the CREATEs
carry the columns) and ``_migrate_add_columns`` (the create_all_tables base
path), per schema_both_build_paths. The runner owns the transaction — no commit
here.
"""

from __future__ import annotations

import aiosqlite

_COLUMNS = (
    ("session_charters", "mission_updated_at"),
    ("cc_sessions", "topic_updated_at"),
)


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608 — literal, ours
    return {row[1] for row in await cursor.fetchall()}


async def up(db: aiosqlite.Connection) -> None:
    for table, column in _COLUMNS:
        if not await _table_exists(db, table):
            continue  # fresh DB — create_all_tables already carries the column
        if column not in await _columns(db, table):
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")  # noqa: S608


async def down(db: aiosqlite.Connection) -> None:
    for table, column in _COLUMNS:
        if not await _table_exists(db, table):
            continue
        if column in await _columns(db, table):
            await db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")  # noqa: S608
