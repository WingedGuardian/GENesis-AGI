"""cc_sessions.satellite_id — fresh-DB column parity + voice-session threading.

The column is added via ``_migrate_add_columns`` only (no numbered migration),
mirroring ``last_extracted_at``/``last_extracted_line``. ``create_all_tables``
runs ``_migrate_add_columns``, so a FRESH build must carry the column — that is
the ``schema_both_build_paths`` guarantee. Also proves the satellite id threads
end-to-end from the transcript writer's ``append_message`` into the row.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.channels.voice.transcript_writer import (
    VoiceTranscriptWriter,
    transcript_session_id,
)
from genesis.db.crud import cc_sessions as ccs
from genesis.db.schema import create_all_tables

pytestmark = pytest.mark.asyncio


async def _fresh_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    return conn


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608 - fixed identifier
    return {row[1] for row in await cur.fetchall()}


async def test_fresh_db_has_satellite_id_column():
    db = await _fresh_db()
    try:
        assert "satellite_id" in await _columns(db, "cc_sessions")
    finally:
        await db.close()


async def test_register_persists_satellite_id():
    db = await _fresh_db()
    try:
        await ccs.register_voice_session(
            db,
            id="sid-a",
            started_at="2026-08-11T00:00:00+00:00",
            satellite_id="kitchen",
        )
        cur = await db.execute("SELECT satellite_id, source_tag FROM cc_sessions WHERE id='sid-a'")
        row = await cur.fetchone()
        assert row["satellite_id"] == "kitchen"
        assert row["source_tag"] == "voice"
    finally:
        await db.close()


async def test_register_without_satellite_id_is_null():
    db = await _fresh_db()
    try:
        await ccs.register_voice_session(db, id="sid-b", started_at="2026-08-11T00:00:00+00:00")
        cur = await db.execute("SELECT satellite_id FROM cc_sessions WHERE id='sid-b'")
        row = await cur.fetchone()
        assert row["satellite_id"] is None
    finally:
        await db.close()


async def test_append_message_threads_satellite_id(tmp_path):
    """The device id survives the full write path into the row (Level-3 wiring)."""
    db = await _fresh_db()
    try:
        writer = VoiceTranscriptWriter(db, transcript_dir=tmp_path)
        ext = "s2s-kitchen-20260811-000000"
        await writer.append_message(ext, "user", "hello there", satellite_id="kitchen")
        cur = await db.execute(
            "SELECT satellite_id FROM cc_sessions WHERE id=?",
            (transcript_session_id(ext),),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["satellite_id"] == "kitchen"
    finally:
        await db.close()
