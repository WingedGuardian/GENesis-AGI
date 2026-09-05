"""Migration 0091: the two timestamps the peer-line topic recency comparison needs.

Both columns exist because neither table had a timestamp meaning what the
comparison requires:

  * ``session_charters.updated_at`` is a ROW timestamp — ``set_pointers`` and the
    charter upsert bump it too, so it cannot say when the MISSION was set.
  * ``cc_sessions.last_extracted_at`` is a PASS watermark — a different CRUD
    function writes it, and the extraction job advances it even on passes that
    write no topic, so it cannot say when the TOPIC was written.

Using either as the other's proxy puts the same defect on one side of the
comparison, which is exactly what the first version of this work did.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

m0091 = importlib.import_module("genesis.db.migrations.0091_topic_recency_stamps")


async def _legacy_db() -> aiosqlite.Connection:
    """Pre-0091 shapes for both tables."""
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE session_charters (
            session_id TEXT PRIMARY KEY, transcript_path TEXT, origin_prompt TEXT,
            origin_ts TEXT, mission TEXT, pointers TEXT NOT NULL DEFAULT '[]',
            compaction_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            updated_at TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE cc_sessions (
            id TEXT PRIMARY KEY, session_type TEXT NOT NULL, model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', started_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL, topic TEXT, keywords TEXT,
            last_extracted_at TEXT, last_extracted_line INTEGER DEFAULT 0
        )"""
    )
    await db.commit()
    return db


async def _cols(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_adds_both_columns():
    db = await _legacy_db()
    try:
        await m0091.up(db)
        assert "mission_updated_at" in await _cols(db, "session_charters")
        assert "topic_updated_at" in await _cols(db, "cc_sessions")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_is_idempotent():
    db = await _legacy_db()
    try:
        await m0091.up(db)
        await m0091.up(db)  # duplicate ADDs must not raise
        assert "mission_updated_at" in await _cols(db, "session_charters")
        assert "topic_updated_at" in await _cols(db, "cc_sessions")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_is_a_noop_when_the_tables_do_not_exist():
    """A fresh DB gets both columns from create_all_tables, so arriving before
    the tables exist must not fail the whole migration run."""
    db = await aiosqlite.connect(":memory:")
    try:
        await m0091.up(db)  # must not raise
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_partially_applied_run_completes():
    """One column already present must not stop the other being added."""
    db = await _legacy_db()
    try:
        await db.execute("ALTER TABLE session_charters ADD COLUMN mission_updated_at TEXT")
        await m0091.up(db)
        assert "topic_updated_at" in await _cols(db, "cc_sessions")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_existing_rows_read_null_rather_than_a_backfilled_time():
    """The load-bearing property: NO backfill, on EITHER column.

    For a pre-migration row we do not know when the mission was set or when the
    topic was written. An invented-recent stamp on either side would silently
    reorder the peer line; NULL means "cannot compare" and the consumer keeps
    its prior ordering, so the migration is inert on arrival.
    """
    db = await _legacy_db()
    try:
        await db.execute(
            "INSERT INTO session_charters (session_id, mission, created_at, updated_at) "
            "VALUES ('s1', 'a mission of unknown age', '2026-01-01', '2026-06-01')"
        )
        await db.execute(
            "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at, "
            "topic, last_extracted_at) VALUES ('s1', 'foreground', 'opus', '2026-01-01', "
            "'2026-01-01', 'a topic of unknown age', '2026-06-01')"
        )
        await m0091.up(db)
        cur = await db.execute(
            "SELECT mission_updated_at FROM session_charters WHERE session_id='s1'"
        )
        assert (await cur.fetchone())[0] is None
        cur = await db.execute("SELECT topic_updated_at FROM cc_sessions WHERE id='s1'")
        assert (await cur.fetchone())[0] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_down_drops_both_columns():
    db = await _legacy_db()
    try:
        await m0091.up(db)
        await m0091.down(db)
        assert "mission_updated_at" not in await _cols(db, "session_charters")
        assert "topic_updated_at" not in await _cols(db, "cc_sessions")
    finally:
        await db.close()


# -- wiring: a column nothing writes is the same as no column ----------------


@pytest.mark.asyncio
async def test_set_mission_stamps_the_mission_column():
    from genesis.db.crud import session_charters as crud

    db = await _legacy_db()
    try:
        await m0091.up(db)
        await db.execute(
            "INSERT INTO session_charters (session_id, created_at) VALUES ('s1', '2026-01-01')"
        )
        await db.commit()
        assert await crud.set_mission(db, "s1", "a freshly declared mission") is True
        cur = await db.execute(
            "SELECT mission_updated_at, updated_at FROM session_charters WHERE session_id='s1'"
        )
        mission_at, updated_at = await cur.fetchone()
        assert mission_at is not None, "set_mission left the new column NULL"
        assert mission_at == updated_at, "both timestamps come from one clock read"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_update_topic_and_keywords_stamps_the_topic_column():
    from genesis.db.crud import cc_sessions as crud

    db = await _legacy_db()
    try:
        await m0091.up(db)
        await db.execute(
            "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at) "
            "VALUES ('s1', 'foreground', 'opus', '2026-01-01', '2026-01-01')"
        )
        await db.commit()
        assert (
            await crud.update_topic_and_keywords(db, "s1", topic="new topic", keywords="a,b")
            is True
        )
        cur = await db.execute("SELECT topic, topic_updated_at FROM cc_sessions WHERE id='s1'")
        topic, topic_at = await cur.fetchone()
        assert topic == "new topic"
        assert topic_at is not None, "the topic write left its stamp NULL"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_watermark_write_does_NOT_stamp_the_topic():
    """The decoupling that motivated the second column, pinned.

    ``update_extraction_watermark`` advances ``last_extracted_at`` on every
    extraction pass, including passes that write no topic. If it ever also
    stamped ``topic_updated_at``, the peer line would treat a
    produced-no-topic pass as a fresh topic and suppress a genuinely newer
    mission -- the exact defect this column exists to avoid.
    """
    from genesis.db.crud import cc_sessions as crud

    db = await _legacy_db()
    try:
        await m0091.up(db)
        await db.execute(
            "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at) "
            "VALUES ('s1', 'foreground', 'opus', '2026-01-01', '2026-01-01')"
        )
        await db.commit()
        await crud.update_topic_and_keywords(db, "s1", topic="t", keywords="k")
        cur = await db.execute("SELECT topic_updated_at FROM cc_sessions WHERE id='s1'")
        before = (await cur.fetchone())[0]

        await crud.update_extraction_watermark(
            db, "s1", last_extracted_line=99, last_extracted_at="2099-01-01T00:00:00+00:00"
        )
        cur = await db.execute(
            "SELECT topic_updated_at, last_extracted_at FROM cc_sessions WHERE id='s1'"
        )
        after, watermark = await cur.fetchone()
        assert watermark == "2099-01-01T00:00:00+00:00", "the watermark did not advance"
        assert after == before, "the watermark write moved the TOPIC stamp"
    finally:
        await db.close()
